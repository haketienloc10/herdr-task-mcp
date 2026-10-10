"""Codex review #5478847114: reserve Discovery output capacity before Peer launch."""
import asyncio
import importlib

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from test_task_readiness import workspace
from qiqi_delegate.task_request import TaskRequestStore


def _setup(tmp_path, monkeypatch, source_count=15):
    runtime, store = workspace(tmp_path)
    monkeypatch.setenv("QIQI_WORKSPACE_ROOT", str(runtime.root))
    server = importlib.import_module("qiqi_delegate.server")
    monkeypatch.setattr(server, "runtime", runtime)
    monkeypatch.setattr(server, "task_requests", store)
    request = store.create(
        "Investigate order idempotency",
        sources=[
            {"kind": "inline", "label": f"source-{n}", "text": f"Context {n}"}
            for n in range(source_count)
        ],
    )
    store.assess(request["request_id"], 1, {
        "requirements": [{"id": "R1", "text": "Discover order idempotency",
                          "evidence_refs": ["request:current"]}],
        "blocking_unknowns": ["Unknown state of backend idempotency"],
        "decision": "targeted_discovery",
        "rationale": "Need to inspect backend before implementing",
    })
    return server, runtime, store, request["request_id"]


def _save_captured(runtime, turn_id="discovery-captured", text="Backend source evidence: src/orders.py:52"):
    with runtime._connect() as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            (turn_id, "native-session", "backend", "codex-balanced",
             "settled", text, 123),
        )


def test_full_source_context_blocks_discovery_before_peer_launch(tmp_path, monkeypatch):
    server, runtime, store, request_id = _setup(tmp_path, monkeypatch, 16)
    called = []

    async def fake_delegate(**kwargs):
        called.append(kwargs)
        raise AssertionError("Discovery Peer must never be launched at capacity")

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    with pytest.raises(ToolError, match="no free context source slot"):
        asyncio.run(server.delegate_discovery(
            request_id, ["backend"], "codex-balanced",
            ["Trace idempotency with file:line"],
        ))
    assert called == []
    state = store.get(request_id)
    assert state["revision"] == 2
    assert len(state["sources"]) == 16
    assert state["discoveries"] == []


def test_fifteenth_source_reserves_one_slot_for_native_capture(tmp_path, monkeypatch):
    server, runtime, store, request_id = _setup(tmp_path, monkeypatch, 15)

    async def fake_delegate(**kwargs):
        _save_captured(runtime)
        # Simulate a second session trying to append during the running Peer.
        with pytest.raises(ValueError, match="reserved"):
            store.append(request_id, 2, {"kind": "inline", "text": "late source"})
        return {
            "state": "settled", "turn_id": "discovery-captured",
            "agent_response": "Backend source evidence: src/orders.py:52",
        }

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    result = asyncio.run(server.delegate_discovery(
        request_id, ["backend"], "codex-balanced",
        ["Trace idempotency with file:line"],
    ))
    assert result["attachment_error"] is None
    assert result["task_request"]["revision"] == 3
    assert result["task_request"]["assessment"] is None
    assert len(result["task_request"]["sources"]) == 16
    assert result["task_request"]["sources"][-1]["kind"] == "peer_turn"
    assert result["task_request"]["discoveries"][0]["state"] == "settled"

    reloaded = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    assert reloaded.get(request_id)["sources"][-1]["turn_id"] == "discovery-captured"


def test_multiple_discoveries_reserve_distinct_slots(tmp_path):
    runtime, store = workspace(tmp_path)
    request = store.create(
        "Inspect ownership and idempotency",
        sources=[{"kind": "inline", "text": "context"} for _ in range(14)],
    )
    store.assess(request["request_id"], 1, {
        "requirements": [{"id": "R1", "text": "Inspect ownership",
                          "evidence_refs": ["request:current"]}],
        "blocking_unknowns": ["Ownership unknown"],
        "decision": "targeted_discovery",
        "rationale": "Need to inspect owner",
    })
    rid = request["request_id"]
    first = store.begin_discovery(rid, "targeted_discovery",
                                  ["backend"], ["Find owner"], "codex-balanced")
    second = store.begin_discovery(rid, "targeted_discovery",
                                   ["frontend"], ["Find consumer"], "codex-balanced")
    with pytest.raises(ValueError, match="no free context source slot"):
        store.begin_discovery(rid, "targeted_discovery",
                              ["backend"], ["Extra question"], "codex-balanced")
    with pytest.raises(ValueError, match="reserved"):
        store.append(rid, 2, {"kind": "inline", "text": "late"})
    store.finish_discovery(first, "failed", detail="Peer could not connect")
    # Released reservation can be used by a different Discovery.
    third = store.begin_discovery(rid, "targeted_discovery",
                                  ["backend"], ["Retry owner"], "codex-balanced")
    assert third not in {first, second}


def test_attachment_failure_still_returns_captured_response(tmp_path, monkeypatch):
    server, runtime, store, request_id = _setup(tmp_path, monkeypatch, 15)
    body = "Important verified evidence: backend/orders.py:35"

    async def fake_delegate(**kwargs):
        _save_captured(runtime, "failure-turn", body)
        return {"state": "settled", "turn_id": "failure-turn",
                "agent_response": body}

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    monkeypatch.setattr(
        store, "append",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("stale task context revision")
        ),
    )
    result = asyncio.run(server.delegate_discovery(
        request_id, ["backend"], "codex-balanced", ["Inspect error"],
    ))
    assert result["result"]["agent_response"] == body
    assert result["result"]["turn_id"] == "failure-turn"
    assert "stale task context revision" in result["attachment_error"]
    assert result["task_request"] is None
    assert store.get(request_id)["discoveries"][0]["state"] == "failed"
    assert store.get(request_id)["discoveries"][0]["turn_id"] == "failure-turn"
