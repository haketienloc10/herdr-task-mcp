"""Review #5479551861: stale request races, oversized intent, metadata collisions."""
import asyncio
import json
import sqlite3
import subprocess

import pytest

from qiqi_delegate.final_eval_manifest import inspect_roots
from test_final_evaluation import setup_graph, report


def _native_capture(runtime, turn_id, body):
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            (turn_id, "final-session", "backend", "codex-evaluator",
             "settled", body, 987),
        )



@pytest.mark.parametrize("new_requirement_id", ["R1", "R2"])
def test_changed_task_request_mid_evaluation_terminalizes_and_can_retry(
    tmp_path, monkeypatch, new_requirement_id,
):
    runtime, requests, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    request_id = requests.graph_binding(gid)["request_id"]
    calls = []

    async def first_delegate(**kwargs):
        calls.append(kwargs["evaluation_id"])
        coordinator.store.bind_turn(kwargs["evaluation_id"], "stale-capture")
        # The original assessment is still R1 when the Evaluator started.
        # Lead changes it while the native worker is running. Even if the
        # requirement IDs happen to remain equal, its revision has changed.
        revision = requests.get(request_id)["revision"]
        changed = requests.assess(request_id, revision, {
            "requirements": [{
                "id": new_requirement_id,
                "text": "Updated user acceptance criterion",
                "evidence_refs": ["request:current"],
            }],
            "blocking_unknowns": [],
            "decision": "direct",
            "rationale": "Newer requirement review from user",
        })
        assert changed["revision"] == revision + 1
        frozen = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        body = json.dumps(report(frozen))
        _native_capture(runtime, "stale-capture", body)
        return {
            "state": "settled", "turn_id": "stale-capture",
            "agent_response": body,
        }

    monkeypatch.setattr(runtime, "delegate", first_delegate)
    graph_revision = graph_runtime.get_graph(gid)["revision"]
    result = asyncio.run(
        coordinator.start(gid, "codex-evaluator", graph_revision)
    )
    assert result["status"] == "inconclusive"
    assert "Stale final evaluation" in result["detail"]
    assert result["is_current"] is False
    assert result["turn_id"] == "stale-capture"
    assert result["raw_response"]
    assert result["report"]["verdict"] == "pass"
    assert coordinator.store.get(result["evaluation_id"])["native_capture"]["state"] == "settled"
    with coordinator.store._connect() as db:
        active = db.execute(
            "SELECT COUNT(*) FROM final_evaluations "
            "WHERE graph_run_id=? AND status IN ('requested','evaluating')",
            (gid,),
        ).fetchone()[0]
    assert active == 0

    # Rebind the updated assessment, then launch one fresh whole-graph
    # evaluation. The stale attempt cannot monopolize the active slot.
    updated = requests.get(request_id)
    binding = requests.graph_binding(gid)
    updated_map = {
        node: [new_requirement_id]
        for node in binding["requirement_map"]
    }
    requests.bind_graph(
        gid, request_id, updated["revision"],
        list(updated_map), updated_map, replace=True,
    )

    async def second_delegate(**kwargs):
        calls.append(kwargs["evaluation_id"])
        coordinator.store.bind_turn(kwargs["evaluation_id"], "fresh-capture")
        manifest = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        payload = report(manifest)
        payload["requirement_results"][0]["requirement_id"] = new_requirement_id
        body = json.dumps(payload)
        _native_capture(runtime, "fresh-capture", body)
        return {
            "state": "settled", "turn_id": "fresh-capture",
            "agent_response": body,
        }

    monkeypatch.setattr(runtime, "delegate", second_delegate)
    renewed = asyncio.run(
        coordinator.start(gid, "codex-evaluator", graph_revision)
    )
    assert renewed["status"] == "passed"
    assert renewed["is_current"] is True
    assert len(calls) == 2
    assert renewed["evaluation_id"] != result["evaluation_id"]

