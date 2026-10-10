"""Regressions for Codex review #5478967179: cancellation and recovery races."""
import asyncio
import importlib
import sqlite3

import pytest

from test_task_readiness import workspace
from qiqi_delegate.task_request import TaskRequestStore


DEAD_PID = 987654321


def _request(store, count=14):
    request = store.create(
        "Investigate payments",
        sources=[{"kind": "inline", "text": f"Context {i}"} for i in range(count)],
    )
    store.assess(request["request_id"], 1, {
        "requirements": [{
            "id": "R1",
            "text": "Inspect payment service behavior",
            "evidence_refs": ["request:current"],
        }],
        "blocking_unknowns": ["Payment behavior not yet verified"],
        "decision": "targeted_discovery",
        "rationale": "Need runtime evidence for implementation plan",
    })
    return request["request_id"]


def _reserve(store, rid):
    return store.begin_discovery(
        rid, "targeted_discovery", ["backend"],
        ["Trace payments and cite file:line"], "codex-balanced",
    )


def _native_capture(runtime, did, tid):
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tid, "native-session", "backend", "codex-balanced",
             "settled", f"Verified contract at backend/payments.py:42 ({tid})", 111),
        )
        db.execute(
            "UPDATE task_discoveries SET turn_id=?, owner_pid=?, "
            "owner_start_token=? WHERE discovery_id=?",
            (tid, DEAD_PID, "dead-owner", did),
        )


def test_cancelled_discovery_preserves_prebound_native_turn(tmp_path, monkeypatch):
    runtime, store = workspace(tmp_path)
    monkeypatch.setenv("QIQI_WORKSPACE_ROOT", str(runtime.root))
    server = importlib.import_module("qiqi_delegate.server")
    monkeypatch.setattr(server, "task_requests", store)
    monkeypatch.setattr(server, "runtime", runtime)
    rid = _request(store, 15)

    async def cancel_after_persist(**kwargs):
        did = kwargs["discovery_id"]
        tid = "captured-before-cancel"
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "UPDATE task_discoveries SET turn_id=? WHERE discovery_id=?",
                (tid, did),
            )
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tid, "native-session", "backend", "codex-balanced",
                 "settled", "Verified cancellation-tail evidence: src/payments.py:32", 111),
            )
        raise asyncio.CancelledError()

    monkeypatch.setattr(runtime, "delegate", cancel_after_persist)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(server.delegate_discovery(
            rid, ["backend"], "codex-balanced", ["Trace payments"],
        ))

    observed = store.get(rid)
    assert observed["discoveries"][0]["state"] == "failed"
    assert observed["discoveries"][0]["turn_id"] == "captured-before-cancel"
    assert len(observed["sources"]) == 15

    # The Lead can recover the durable captured result using the exposed ID.
    recovered = store.append(
        rid, observed["revision"],
        {"kind": "peer_turn", "turn_id": observed["discoveries"][0]["turn_id"]},
    )
    assert recovered["sources"][-1]["turn_id"] == "captured-before-cancel"
    assert "cancellation-tail evidence" in recovered["sources"][-1]["content"]


def test_finish_discovery_rejects_mismatched_bound_turn(tmp_path):
    runtime, store = workspace(tmp_path)
    rid = _request(store)
    did = _reserve(store, rid)
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "UPDATE task_discoveries SET turn_id='correct-turn' WHERE discovery_id=?",
            (did,),
        )
    with pytest.raises(ValueError, match="unknown or completed"):
        store.finish_discovery(did, "failed", turn_id="wrong-turn")
    row = store.get(rid)["discoveries"][0]
    assert row["state"] == "requested"
    assert row["turn_id"] == "correct-turn"


def test_recovery_before_append_does_not_transition_unwritten_source(tmp_path):
    runtime, store = workspace(tmp_path)
    rid = _request(store, 14)
    old = _reserve(store, rid)
    active = _reserve(store, rid)
    _native_capture(runtime, old, "dead-native-result")

    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("active-native-result", "native-session", "backend",
             "codex-balanced", "settled", "Active source at src/payments.py:80", 123),
        )
        db.execute(
            "UPDATE task_discoveries SET turn_id=? WHERE discovery_id=?",
            ("active-native-result", active),
        )
    # append() must recover old's settled capture first, then check revision.
    # The active result must not be marked attaching when its own UPDATE cannot commit.
    with pytest.raises(RuntimeError, match="stale task context revision"):
        store.append(
            rid, 2, {"kind": "peer_turn", "turn_id": "active-native-result"},
            discovery_id=active,
        )

    fresh = TaskRequestStore(runtime.db, runtime.root, runtime.repos).get(rid)
    assert fresh["revision"] == 3
    assert fresh["assessment"] is None
    assert len(fresh["sources"]) == 15
    assert fresh["sources"][-1]["turn_id"] == "dead-native-result"
    states = {d["discovery_id"]: d["state"] for d in fresh["discoveries"]}
    assert states[old] == "settled"
    assert states[active] == "requested"


def test_begin_discovery_checks_capacity_and_readiness_after_recovery(tmp_path):
    runtime, store = workspace(tmp_path)
    rid = _request(store, 15)
    old = _reserve(store, rid)
    _native_capture(runtime, old, "dead-captured-turn")

    # Initial request is still revision 2, has 15 sources and is assessable.
    # Recovery during begin_discovery changes it to revision 3 with 16 sources.
    with pytest.raises(RuntimeError, match="reassess before dispatch"):
        _reserve(store, rid)

    current = TaskRequestStore(runtime.db, runtime.root, runtime.repos).get(rid)
    assert current["revision"] == 3
    assert current["assessment"] is None
    assert len(current["sources"]) == 16
    assert current["sources"][-1]["turn_id"] == "dead-captured-turn"
    assert len(current["discoveries"]) == 1
    assert current["discoveries"][0]["state"] == "settled"

    # Recovery cannot be bypassed by a retry with the original assessment.
    with pytest.raises(ValueError, match="matching readiness decision"):
        _reserve(store, rid)
