"""Codex review #5478870761: crash recovery for Discovery source reservations."""
import asyncio
import sqlite3

import pytest

from test_task_readiness import workspace
from qiqi_delegate.task_request import TaskRequestStore


DEAD_PID = 987654321


def _request(store, count=15):
    request = store.create(
        "Investigate idempotency across repositories",
        sources=[{"kind": "inline", "text": f"context {n}"} for n in range(count)],
    )
    store.assess(request["request_id"], 1, {
        "requirements": [{"id": "R1", "text": "Inspect idempotency",
                          "evidence_refs": ["request:current"]}],
        "blocking_unknowns": ["Idempotency flow unknown"],
        "decision": "targeted_discovery",
        "rationale": "Requires investigation before implementation",
    })
    return request["request_id"]


def _reserve(store, request_id):
    return store.begin_discovery(
        request_id, "targeted_discovery", ["backend"],
        ["Trace idempotency with file:line"], "codex-balanced",
    )


def _simulate_process_exit(database, discovery_id):
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE task_discoveries SET owner_pid=?, owner_start_token=? "
            "WHERE discovery_id=?",
            (DEAD_PID, "never-alive", discovery_id),
        )


def test_restart_recovers_dead_request_and_frees_reserved_slot(tmp_path):
    runtime, store = workspace(tmp_path)
    rid = _request(store)
    discovery = _reserve(store, rid)
    assert len(store.get(rid)["sources"]) == 15
    with pytest.raises(ValueError, match="reserved"):
        store.append(rid, 2, {"kind": "inline", "text": "new source"})

    # A second store, representing a separate still-running server, must NOT
    # free the live server's reservation.
    live_reader = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    assert live_reader.get(rid)["discoveries"][0]["state"] == "requested"
    with pytest.raises(ValueError, match="reserved"):
        live_reader.append(rid, 2, {"kind": "inline", "text": "another source"})

    _simulate_process_exit(runtime.db, discovery)
    restarted = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    state = restarted.get(rid)
    assert state["discoveries"][0]["state"] == "interrupted"
    assert "abandoned source reservation recovered" in state["discoveries"][0]["detail"]
    assert restarted.append(rid, 2, {"kind": "inline", "text": "recovered source"})[
        "revision"] == 3
    assert len(restarted.get(rid)["sources"]) == 16


def test_new_discovery_can_start_after_abandoned_claim_is_recovered(tmp_path):
    runtime, store = workspace(tmp_path)
    rid = _request(store)
    old = _reserve(store, rid)
    _simulate_process_exit(runtime.db, old)

    # Recovery is also run at the capacity gate, not only at initialization.
    new = _reserve(store, rid)
    assert new != old
    statuses = {x["discovery_id"]: x["state"] for x in store.get(rid)["discoveries"]}
    assert statuses[old] == "interrupted"
    assert statuses[new] == "requested"
    with pytest.raises(ValueError, match="no free context source slot"):
        _reserve(store, rid)


def test_restart_finalizes_durably_attached_capture_without_duplicate(tmp_path):
    runtime, store = workspace(tmp_path)
    rid = _request(store)
    discovery = _reserve(store, rid)
    with runtime._connect() as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("captured-before-crash", "native-session", "backend", "codex-balanced",
             "settled", "Verified at src/orders.py:54", 1234),
        )

    # Simulate a crash after the atomic append/attaching transaction, but
    # before finish_discovery can mark it settled.
    attached = store.append(
        rid, 2, {"kind": "peer_turn", "turn_id": "captured-before-crash"},
        discovery_id=discovery,
    )
    assert attached["revision"] == 3
    assert attached["discoveries"][0]["state"] == "attaching"
    assert attached["discoveries"][0]["turn_id"] == "captured-before-crash"
    _simulate_process_exit(runtime.db, discovery)

    restarted = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    state = restarted.get(rid)
    assert state["revision"] == 3  # Recovery does not change task context.
    assert state["discoveries"][0]["state"] == "settled"
    assert state["discoveries"][0]["turn_id"] == "captured-before-crash"
    assert len(state["sources"]) == 16
    assert state["sources"][-1]["content"] == "Verified at src/orders.py:54"


def test_pid_reuse_token_mismatch_reclaims_reservation(tmp_path):
    runtime, store = workspace(tmp_path)
    rid = _request(store)
    discovery = _reserve(store, rid)
    # The PID is live but its stored process birth token does not match.
    # On Linux this handles PID reuse after the MCP server has exited.
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "UPDATE task_discoveries SET owner_start_token='wrong-process-birth' "
            "WHERE discovery_id=?",
            (discovery,),
        )
    from qiqi_delegate.task_request import _process_start_token
    import os
    if _process_start_token(os.getpid()) is None:
        pytest.skip("process birth token unavailable on this platform")
    recovered = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    assert recovered.get(rid)["discoveries"][0]["state"] == "interrupted"


def test_pre_migration_unowned_reservation_recovers(tmp_path):
    runtime, store = workspace(tmp_path)
    rid = _request(store)
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO task_discoveries "
            "(discovery_id, request_id, mode, repository_names_json, "
            "questions_json, route, state, created_at_ns) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-reservation", rid, "targeted_discovery",
             '["backend"]', '["Inspect"]', "codex-balanced", "requested", 1),
        )
    new_store = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    assert new_store.get(rid)["discoveries"][0]["state"] == "interrupted"
    assert len(new_store.append(
        rid, 2, {"kind": "inline", "text": "unblocked"},
    )["sources"]) == 16



def test_restart_recovers_settled_turn_captured_before_source_append(tmp_path):
    """The last review's crash window: turns INSERT committed, append not called."""
    runtime, store = workspace(tmp_path)
    rid = _request(store)
    did = _reserve(store, rid)
    long_result = "E" * 130_000 + " CRITICAL_CONTRACT: Idempotency-Key"
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("native-just-finished", "session-n", "backend", "codex-balanced",
             "settled", long_result, 1234),
        )
        db.execute(
            "UPDATE task_discoveries SET turn_id=? WHERE discovery_id=?",
            ("native-just-finished", did),
        )
    # Crash happens here: runtime.delegate committed the turn but the server
    # did not get to task_requests.append() or finish_discovery().
    _simulate_process_exit(runtime.db, did)
    restarted = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    request = restarted.get(rid)
    assert request["revision"] == 3
    assert request["assessment"] is None
    assert request["discoveries"][0]["state"] == "settled"
    assert request["discoveries"][0]["turn_id"] == "native-just-finished"
    assert len(request["sources"]) == 16
    source = request["sources"][-1]
    assert source["kind"] == "peer_turn"
    assert source["turn_id"] == "native-just-finished"
    assert source["content"] == long_result
    assert source["content"].endswith("CRITICAL_CONTRACT: Idempotency-Key")

    # Idempotent recovery: source isn't added a second time.
    restarted_again = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    assert restarted_again.get(rid)["revision"] == 3
    assert len(restarted_again.get(rid)["sources"]) == 16


def test_live_owner_turn_does_not_get_recovered_prematurely(tmp_path):
    runtime, store = workspace(tmp_path)
    rid = _request(store)
    did = _reserve(store, rid)
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("live-unattached", "session-n", "backend", "codex-balanced",
             "settled", "source:line contract", 1234),
        )
        db.execute(
            "UPDATE task_discoveries SET turn_id=? WHERE discovery_id=?",
            ("live-unattached", did),
        )
    another_store = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    request = another_store.get(rid)
    assert request["revision"] == 2
    assert request["discoveries"][0]["state"] == "requested"
    assert len(request["sources"]) == 15
    with pytest.raises(ValueError, match="reserved"):
        another_store.append(rid, 2, {"kind": "inline", "text": "must wait"})


def test_crash_before_native_turn_commit_marks_interrupted(tmp_path):
    runtime, store = workspace(tmp_path)
    rid = _request(store)
    did = _reserve(store, rid)
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "UPDATE task_discoveries SET turn_id=? WHERE discovery_id=?",
            ("turn-never-captured", did),
        )
    _simulate_process_exit(runtime.db, did)
    recovered = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    request = recovered.get(rid)
    assert request["revision"] == 2
    assert request["discoveries"][0]["state"] == "interrupted"
    assert request["discoveries"][0]["turn_id"] == "turn-never-captured"
    assert len(request["sources"]) == 15
    assert recovered.append(
        rid, 2, {"kind": "inline", "text": "retry possible"}
    )["revision"] == 3


def test_runtime_binds_discovery_turn_id_before_launch(tmp_path, monkeypatch):
    import qiqi_delegate.runtime as runtime_module
    from qiqi_delegate.core import build_task_packet

    runtime, store = workspace(tmp_path)
    rid = _request(store)
    did = _reserve(store, rid)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda _: "/bin/herdr")

    def intercept_claim(repository, claim_id, *, repository_root):
        # The invocation has not launched Herdr, but there already is a durable
        # Discovery association with the turn allocated for this dispatch.
        with sqlite3.connect(runtime.db) as db:
            saved = db.execute(
                "SELECT turn_id, state FROM task_discoveries WHERE discovery_id=?",
                (did,),
            ).fetchone()
        assert saved[1] == "requested"
        assert saved[0] and claim_id == "turn:" + saved[0]
        raise RuntimeError("stop before Herdr launch")

    monkeypatch.setattr(runtime, "_claim", intercept_claim)
    packet = build_task_packet(
        objective="Inspect flow", scope=["backend"],
        acceptance_criteria=["Report file:line"],
    )
    with pytest.raises(RuntimeError, match="stop before Herdr launch"):
        asyncio.run(runtime.delegate(
            repository="backend", route="codex-balanced", packet=packet,
            discovery_repositories=("backend",), discovery_id=did,
        ))
    assert store.get(rid)["discoveries"][0]["turn_id"] is not None
