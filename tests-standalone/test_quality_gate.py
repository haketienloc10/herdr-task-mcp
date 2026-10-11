"""Issue #11: unfinished/unverified work must never become silent ACCEPT/PASS."""
import asyncio
import json

import pytest

from qiqi_delegate import quality
from qiqi_delegate.final_eval_store import FinalEvaluationStore
from qiqi_delegate.task_graph_runtime import (
    GraphRuntime, decisions_from_payload, task_graph_from_payload,
)
from qiqi_delegate.task_graph_store import GraphRuntimeStore


def graph(node="work"):
    return task_graph_from_payload({"nodes": [{
        "node_id": node, "repository": "backend", "route": "codex",
        "task_packet": {"objective": "Implement and test retry", "scope": ["src"],
                        "acceptance_criteria": ["Retry 429", "Regression tests pass"]},
    }]})


def peer_report(*, status="done", unresolved=None, resolved=None, index=None):
    def criterion(i):
        return {
            "criterion_index": i, "status": status if index in (None, i) else "done",
            "evidence": [{"kind": "repo_file", "repository": "backend",
                          "path": "src/client.py", "locator": "retry:42"}],
            "verification": {
                "method": "source_inspection", "performed": True,
                "outcome": "pass", "artifact_ref": None,
            },
            "details": "Code examined",
        }
    return {
        "schema_version": 1, "summary": "Implementation review",
        "criterion_results": [criterion(0), criterion(1)],
        "unresolved_items": unresolved or [],
        "resolved_items": resolved or [],
    }


def issue(issue_id="missing-check"):
    return {
        "id": issue_id, "kind": "unable_to_verify",
        "description": "Integration test unavailable",
        "affected_requirement_ids": ["R1"], "reason": "No service",
        "attempted_methods": ["Local test"], "next_action": "Use a service fixture",
    }


def runtime(tmp_path, *, item_graph=None):
    path = tmp_path / "graph.sqlite3"
    store = GraphRuntimeStore(path)
    rt = GraphRuntime(store)
    authored = item_graph or graph()
    run = rt.start_graph(authored, repository_names={"backend"})
    quality.seed(path, run["graph_run_id"], "request", 1,
                 [{"id": "R1", "text": "Implement and verify"}],
                 authored, {node.node_id: ["R1"] for node in authored.nodes})
    return rt, run["graph_run_id"]


def delegate(rt, run_id, response):
    async def execute(node):
        return {"state": "settled", "session_id": "sess-" + node.node_id,
                "turn_id": "turn-" + node.node_id + "-" + str(len(response)),
                "agent_response": response}
    return asyncio.run(rt.delegate_next(run_id, executor=execute))


def lead_reviews(turn_id):
    return [{"criterion_index": i, "status": "supported",
             "evidence_refs": ["peer_turn:" + turn_id],
             "rationale": "Inspected evidence in the captured Peer turn"}
            for i in range(2)]


def decision(rt, run_id, snapshot, action, *, reviews=None, feedback=None):
    return rt.submit_decisions(
        run_id,
        decisions_from_payload([{
            "node_id": "work", "action": action,
            **({"criterion_reviews": reviews} if reviews is not None else {}),
            **({"feedback": feedback} if feedback is not None else {}),
        }]),
        expected_revision=snapshot["revision"],
    )


@pytest.mark.parametrize("raw", [
    "Finished", "{}", "", json.dumps({"schema_version": 1}),
])
def test_plain_or_missing_peer_report_never_accepts(tmp_path, raw):
    rt, run = runtime(tmp_path)
    result = delegate(rt, run, raw)
    with pytest.raises(ValueError):
        decision(rt, run, result, "accept", reviews=lead_reviews(result["results"][0]["turn_id"]))
    assert rt.get_graph(run)["graph_state"] == "awaiting_review"
    assert quality.status(rt.store.path, run)["quality_policy"] == "strict_v1"


def test_unverified_criterion_cannot_be_accepted(tmp_path):
    rt, run = runtime(tmp_path)
    report = peer_report(status="not_verified", index=1)
    result = delegate(rt, run, json.dumps(report))
    with pytest.raises(ValueError, match="unfinished/unverified"):
        decision(rt, run, result, "accept", reviews=lead_reviews(result["results"][0]["turn_id"]))
    retry = decision(rt, run, result, "retry", feedback=["Run missing test"])
    assert retry["runnable_nodes"] == ["work"]
    status = quality.status(rt.store.path, run)
    assert any(x["state"] == "not_verified" for x in status["items"])
    assert any(x["state"] == "not_verified" and x["kind"] == "criterion"
               for x in status["items"])


def test_unresolved_issue_survives_retry_and_requires_explicit_resolution(tmp_path):
    rt, run = runtime(tmp_path)
    first = delegate(rt, run, json.dumps(peer_report(unresolved=[issue()])))
    decision(rt, run, first, "retry", feedback=["Verify integration"])
    current = quality.status(rt.store.path, run)
    assert any(x["item_id"] == "discovered:work:missing-check"
               and x["state"] == "unable_to_verify" for x in current["items"])
    second = delegate(rt, run, json.dumps(peer_report()))
    with pytest.raises(ValueError, match="explicitly resolve"):
        decision(rt, run, second, "accept", reviews=lead_reviews(second["results"][0]["turn_id"]))
    # Retry same settled attempt is permitted, but it remains unresolved.
    decision(rt, run, second, "retry", feedback=["Resolve prior issue explicitly"])
    resolution = {
        "id": "discovered:work:missing-check",
        "evidence": [{"kind": "repo_file", "repository": "backend",
                      "path": "tests/integration.py", "locator": "test_retry:10"}],
        "rationale": "Integration check added and reviewed",
    }
    third = delegate(rt, run, json.dumps(peer_report(resolved=[resolution])))
    accepted = decision(rt, run, third, "accept",
                        reviews=lead_reviews(third["results"][0]["turn_id"]))
    assert accepted["graph_state"] == "complete"
    assert quality.status(rt.store.path, run)["quality_state"] == "needs_work"
    # Not independently verified yet, but no work is silently dropped.
    quality.assert_no_open_work(rt.store.path, run)
    with __import__("sqlite3").connect(rt.store.path) as db:
        assert db.execute("SELECT count(*) FROM quality_events WHERE event LIKE 'reported:%'").fetchone()[0] >= 1


def test_replan_rejects_unmapped_missing_work(tmp_path):
    rt, run = runtime(tmp_path)
    first = delegate(rt, run, json.dumps(peer_report(unresolved=[issue()])))
    retry = decision(rt, run, first, "retry")
    next_graph = graph("replacement")
    with pytest.raises(ValueError, match="successor_map"):
        rt.reconcile_graph(run, next_graph, repository_names={"backend"},
                           expected_revision=retry["revision"],
                           requirement_map={"replacement": ["R1"]})
    assert "work" in [n.node_id for n in rt._graph_for_run(run).nodes]
    updated = rt.reconcile_graph(run, next_graph, repository_names={"backend"},
                                 expected_revision=retry["revision"],
                                 requirement_map={"replacement": ["R1"]},
                                 successor_map={"work": "replacement"})
    assert updated["runnable_nodes"] == ["replacement"]
    items = quality.status(rt.store.path, run)["items"]
    assert any(item["kind"] == "discovered"
               and item["node_id"] == "replacement"
               and item["state"] == "unable_to_verify" for item in items)


def test_bound_node_accept_does_not_imply_final_verified(tmp_path):
    rt, run = runtime(tmp_path)
    done = delegate(rt, run, json.dumps(peer_report()))
    accepted = decision(rt, run, done, "accept",
                        reviews=lead_reviews(done["results"][0]["turn_id"]))
    assert accepted["graph_state"] == "complete"
    assert quality.status(rt.store.path, run)["quality_state"] != "verified"
    coverage = quality.final_coverage(rt.store.path, run)
    assert len(coverage["criteria"]) == 2
    assert coverage["discovered"] == {}


def test_v2_final_report_requires_all_quality_criteria_and_manifest_evidence(tmp_path):
    rt, run = runtime(tmp_path)
    done = delegate(rt, run, json.dumps(peer_report()))
    decision(rt, run, done, "accept", reviews=lead_reviews(done["results"][0]["turn_id"]))
    cov = quality.final_coverage(rt.store.path, run)
    digest = "1" * 64
    manifest = {"backend": {"files": [{"path": "src/client.py", "sha256": digest}],
                            "deleted_paths": []}}
    ev = [{"repository": "backend", "path": "src/client.py",
           "sha256": digest, "locator": "retry:42"}]
    report = {
        "schema_version": 2, "verdict": "pass",
        "requirement_results": [{
            "requirement_id": "R1", "status": "pass",
            "evidence": ev, "rationale": "Verified implementation",
        }],
        "criterion_results": [
            {"item_id": cid, "status": "pass",
             "evidence": ev, "rationale": "Verified against source"}
            for cid in cov["criteria"]
        ],
        "resolved_item_results": [], "cross_repository_checks": [],
        "verification_runs": [], "findings": [], "unknowns": [],
    }
    assert FinalEvaluationStore.validate_report(report, manifest, {"R1"}, cov) == report
    bad = dict(report, criterion_results=report["criterion_results"][:1])
    with pytest.raises(ValueError, match="omitted"):
        FinalEvaluationStore.validate_report(bad, manifest, {"R1"}, cov)
    bad = dict(report, unknowns=["Not run"])
    with pytest.raises(ValueError, match="claimed PASS"):
        FinalEvaluationStore.validate_report(bad, manifest, {"R1"}, cov)
    bad = dict(report, criterion_results=[
        dict(item, evidence=[dict(ev[0], sha256="0" * 64)])
        for item in report["criterion_results"]
    ])
    with pytest.raises(ValueError, match="manifest"):
        FinalEvaluationStore.validate_report(bad, manifest, {"R1"}, cov)
