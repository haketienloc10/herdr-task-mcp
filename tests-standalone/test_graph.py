import asyncio
import pytest
from qiqi_delegate.task_graph_runtime import GraphRuntime, task_graph_from_payload, decisions_from_payload
from qiqi_delegate.task_graph_store import GraphRuntimeStore

def make_graph():
    def node(id_, repository, depends_on=None):
        return {
            "node_id": id_, "repository": repository, "route": "claude-balanced",
            "kind": "repo_task", "depends_on": depends_on or [],
            "task_packet": {
                "objective": "Implement " + id_,
                "scope": ["src"],
                "acceptance_criteria": ["Tests pass"],
            }
        }
    return {"nodes": [node("B1", "backend"), node("F1", "frontend", ["B1"])]}

def test_dependency_waits_for_semantic_acceptance(tmp_path):
    graph = task_graph_from_payload(make_graph())
    runtime = GraphRuntime(GraphRuntimeStore(tmp_path / "state.sqlite3"))
    state = runtime.start_graph(graph, repository_names={"backend", "frontend"})
    run_id = state["graph_run_id"]
    assert state["runnable_nodes"] == ["B1"]

    async def execute(node):
        return {"state": "settled", "session_id": "sess_" + node.node_id,
                "turn_id": "turn_" + node.node_id, "agent_response": "Finished"}

    result = asyncio.run(runtime.delegate_next(run_id, executor=execute))
    assert result["graph_state"] == "awaiting_review"
    assert "F1" not in result["runnable_nodes"]
    decisions = decisions_from_payload([{"node_id": "B1", "action": "accept"}])
    updated = runtime.submit_decisions(run_id, decisions, expected_revision=result["revision"],
                                       lead_dispositions=({"turn_id": "turn_B1", "action": "accept",
                                                           "reason": "Evidence reviewed", "node_id": "B1"},))
    assert updated["runnable_nodes"] == ["F1"]
    result = asyncio.run(runtime.delegate_next(run_id, executor=execute))
    assert result["graph_state"] == "awaiting_review"
    assert result["review_required"][0]["node_id"] == "F1"

def test_reject_duplicate_nodes(tmp_path):
    graph = make_graph()
    graph["nodes"].append(graph["nodes"][0])
    runtime = GraphRuntime(GraphRuntimeStore(tmp_path / "state.sqlite3"))
    with pytest.raises(ValueError, match="duplicate"):
        runtime.start_graph(task_graph_from_payload(graph), repository_names={"backend", "frontend"})


def test_ambiguous_capture_requires_review_and_rejects_accept(tmp_path):
    """Ambiguous native captures remain reviewable, never valid ACCEPT evidence."""
    graph = task_graph_from_payload(make_graph())
    runtime = GraphRuntime(GraphRuntimeStore(tmp_path / "state.sqlite3"))
    started = runtime.start_graph(graph, repository_names={"backend", "frontend"})
    run_id = started["graph_run_id"]

    async def execute(node):
        return {
            "state": "capture_ambiguous",
            "session_id": "sess_" + node.node_id,
            "turn_id": "turn_" + node.node_id,
            "agent_response": None,
            "candidate_count": 2,
        }

    result = asyncio.run(runtime.delegate_next(run_id, executor=execute))
    assert result["graph_state"] == "awaiting_review"
    assert result["results"][0]["runtime_state"] == "capture_ambiguous"
    assert result["results"][0]["candidate_count"] == 2
    assert result["runnable_nodes"] == []
    item = result["review_required"][0]
    assert item["node_id"] == "B1"
    assert item["runtime_state"] == "capture_ambiguous"
    assert item["candidate_count"] == 2

    review = runtime.get_node_reviews(
        run_id, [("B1", item["attempt_id"])],
        expected_revision=result["revision"],
    )
    assert len(review["reviews"]) == 1
    assert review["reviews"][0]["result"]["state"] == "capture_ambiguous"
    assert review["reviews"][0]["result"]["agent_response"] is None
    assert review["reviews"][0]["result"]["candidate_count"] == 2

    with pytest.raises(ValueError, match="unambiguous captured Peer evidence"):
        runtime.submit_decisions(
            run_id, decisions_from_payload([{"node_id": "B1", "action": "accept"}]),
            expected_revision=result["revision"],
        )
    assert runtime.get_graph(run_id)["graph_state"] == "awaiting_review"

    updated = runtime.submit_decisions(
        run_id,
        decisions_from_payload([{
            "node_id": "B1", "action": "retry",
            "feedback": ["Collect a single authoritative final response"],
        }]),
        expected_revision=result["revision"],
    )
    assert updated["runnable_nodes"] == ["B1"]
    assert updated["graph_state"] == "ready"
