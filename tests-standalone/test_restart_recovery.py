"""Durable TaskGraph semantics and retry intent across runtime process restarts."""
import asyncio
import sqlite3

import pytest

from qiqi_delegate.task_graph_runtime import (
    GraphRuntime, decisions_from_payload, task_graph_from_payload,
)
from qiqi_delegate.task_graph_store import GraphRuntimeStore, _graph_fingerprint


def make_runtime(path):
    return GraphRuntime(GraphRuntimeStore(path))


def make_graph():
    return task_graph_from_payload({"nodes": [
        {
            "node_id": "B1", "repository": "backend", "route": "codex-balanced",
            "task_packet": {
                "objective": "Implement API",
                "scope": ["src/backend"],
                "acceptance_criteria": ["Tests pass"],
            },
        },
        {
            "node_id": "F1", "repository": "frontend", "route": "claude-balanced",
            "depends_on": ["B1"],
            "task_packet": {
                "objective": "Integrate API",
                "scope": ["src/frontend"],
                "acceptance_criteria": ["Frontend tests pass"],
            },
        },
    ]})


async def settled(node):
    return {
        "state": "settled",
        "session_id": "session_" + node.node_id,
        "turn_id": "turn_" + node.node_id,
        "agent_response": "Evidence for " + node.node_id,
    }


def test_graph_restores_idle_review_accept_and_dependency(tmp_path):
    path = tmp_path / "graph.sqlite3"
    graph = make_graph()
    original = make_runtime(path)
    start = original.start_graph(graph, repository_names={"backend", "frontend"})
    run_id = start["graph_run_id"]
    assert make_runtime(path).get_graph(run_id)["runnable_nodes"] == ["B1"]

    first = asyncio.run(make_runtime(path).delegate_next(run_id, executor=settled))
    item = first["review_required"][0]
    restarted = make_runtime(path)
    assert restarted.get_graph(run_id)["graph_state"] == "awaiting_review"
    batch = restarted.get_node_reviews(
        run_id, [("B1", item["attempt_id"])],
        expected_revision=first["revision"],
    )
    assert batch["reviews"][0]["result"]["agent_response"] == "Evidence for B1"

    accepted = restarted.submit_decisions(
        run_id, decisions_from_payload([{"node_id": "B1", "action": "accept"}]),
        expected_revision=first["revision"],
    )
    assert accepted["runnable_nodes"] == ["F1"]
    later = make_runtime(path)
    assert later.get_graph(run_id)["nodes"][0]["semantic_state"] == "satisfied"
    second = asyncio.run(later.delegate_next(run_id, executor=settled))
    done = make_runtime(path).submit_decisions(
        run_id, decisions_from_payload([{"node_id": "F1", "action": "accept"}]),
        expected_revision=second["revision"],
    )
    assert done["graph_state"] == "complete"
    assert make_runtime(path).get_graph(run_id)["graph_state"] == "complete"
    assert len(make_runtime(path).store.list_attempts(run_id, "B1")) == 1


def test_retry_plan_feedback_and_exact_resume_session_survive_restart(tmp_path):
    path = tmp_path / "graph.sqlite3"
    run_id = make_runtime(path).start_graph(
        make_graph(), repository_names={"backend", "frontend"},
    )["graph_run_id"]
    first = asyncio.run(make_runtime(path).delegate_next(run_id, executor=settled))
    decided = make_runtime(path).submit_decisions(
        run_id,
        decisions_from_payload([{
            "node_id": "B1", "action": "retry", "resume_session": True,
            "feedback": ["Add evidence from src/backend/api.py:42"],
        }]),
        expected_revision=first["revision"],
    )
    assert decided["runnable_nodes"] == ["B1"]

    restored = make_runtime(path)
    node = next(x for x in restored.get_graph(run_id)["nodes"] if x["node_id"] == "B1")
    assert node["retry_pending"] == {
        "resume_session": True,
        "session_id": "session_B1",
        "feedback": ["Add evidence from src/backend/api.py:42"],
    }
    calls = []

    async def start(_node):
        raise AssertionError("retry must resume native session")

    async def resume(node, session_id):
        calls.append((node.node_id, session_id, node.task_packet.as_dict()))
        return await settled(node)

    second = asyncio.run(restored.delegate_next(
        run_id, executor=start, resume_executor=resume,
    ))
    assert second["results"][0]["resume_session"] is True
    assert calls[0][1] == "session_B1"
    assert {
        "claim": "Add evidence from src/backend/api.py:42",
        "source": "QiQi semantic review",
    } in calls[0][2]["context"]["claims_to_investigate"]
    assert make_runtime(path).store.get_retry_plans(run_id) == {}
    assert make_runtime(path).get_graph(run_id)["graph_state"] == "awaiting_review"
    attempts = make_runtime(path).store.list_attempts(run_id, "B1")
    assert len(attempts) == 2
    assert attempts[1]["resume_session"] is True


def test_reconcile_persists_new_graph_and_clears_invalidated_retry(tmp_path):
    path = tmp_path / "graph.sqlite3"
    original = make_runtime(path)
    run_id = original.start_graph(
        make_graph(), repository_names={"backend", "frontend"},
    )["graph_run_id"]
    first = asyncio.run(original.delegate_next(run_id, executor=settled))
    planned = original.submit_decisions(
        run_id, decisions_from_payload([{
            "node_id": "B1", "action": "retry",
            "feedback": ["Missing acceptance proof"],
        }]),
        expected_revision=first["revision"],
    )
    graph_json = make_graph().as_dict()
    graph_json["nodes"][0]["task_packet"]["objective"] = "Implement corrected API"
    graph_json["nodes"].append({
        "node_id": "D1", "repository": "docs", "route": "codex-balanced",
        "depends_on": ["F1"],
        "task_packet": {
            "objective": "Document the change", "scope": ["docs"],
            "acceptance_criteria": ["Documentation complete"],
        },
    })
    replacement = task_graph_from_payload(graph_json)
    updated = make_runtime(path).reconcile_graph(
        run_id, replacement, repository_names={"backend", "frontend", "docs"},
        expected_revision=planned["revision"],
    )
    assert "B1" in updated["reconciliation"]["changed_nodes"]
    assert "F1" in updated["reconciliation"]["dependency_invalidated_nodes"]
    reloaded = make_runtime(path)
    assert reloaded.get_graph(run_id)["authored_node_count"] == 3
    assert reloaded.store.get_retry_plans(run_id) == {}
    assert reloaded.store.load_graph(run_id) == replacement
    assert reloaded.get_graph(run_id)["runnable_nodes"] == ["B1"]


def test_restart_fails_closed_for_running_wave_and_preserves_attempt(tmp_path):
    path = tmp_path / "graph.sqlite3"
    original = make_runtime(path)
    run_id = original.start_graph(
        make_graph(), repository_names={"backend", "frontend"},
    )["graph_run_id"]
    attempt_id = original.store.start_attempt(run_id, "B1", "wave-interrupted")
    restarted = make_runtime(path)
    state = restarted.get_graph(run_id)
    assert state["graph_state"] == "running"
    assert state["current_wave_id"] == "wave-interrupted"
    assert state["nodes"][0]["current_attempt_id"] == attempt_id
    with pytest.raises(RuntimeError, match="not ready"):
        asyncio.run(restarted.delegate_next(run_id, executor=settled))
    assert restarted.store.get_attempt(attempt_id)["runtime_state"] == "running"
    assert len(restarted.store.list_attempts(run_id, "B1")) == 1


def test_corrupt_or_legacy_graph_definition_fails_closed(tmp_path):
    path = tmp_path / "graph.sqlite3"
    runtime = make_runtime(path)
    run_id = runtime.start_graph(
        make_graph(), repository_names={"backend", "frontend"},
    )["graph_run_id"]
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE graph_runs SET graph_json = NULL WHERE graph_run_id = ?", (run_id,))
    with pytest.raises(RuntimeError, match="legacy run"):
        make_runtime(path).get_graph(run_id)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE graph_runs SET graph_json = ? WHERE graph_run_id = ?",
            ('{"nodes":[]}', run_id),
        )
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        make_runtime(path).get_graph(run_id)
