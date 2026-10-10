"""Final Evaluation Gate end-to-end behavior with isolated fake native Evaluator."""
import asyncio
import json
import os
import sqlite3

import pytest

from test_task_readiness import workspace
from qiqi_delegate.final_eval import FinalEvaluationCoordinator
from qiqi_delegate.final_eval_snapshot import (
    EvaluationSnapshot, inspect_roots, manifest_digest,
)
from qiqi_delegate.task_graph_runtime import GraphRuntime, task_graph_from_payload
from qiqi_delegate.task_graph_store import GraphRuntimeStore


def setup_graph(tmp_path, *, multi=True, with_request=True):
    runtime, requests = workspace(tmp_path)
    repos = runtime.repos()
    (repos["backend"] / "orders.py").write_text(
        "def total(n):\n    return n * 2\n", encoding="utf-8",
    )
    (repos["frontend"] / "client.py").write_text(
        "def show(total):\n    return str(total)\n", encoding="utf-8",
    )
    (runtime.root / "agent-routing.yaml").write_text(
        'routes:\n  codex-balanced:\n    agent: codex\n'
        '    args: ["--yolo"]\n'
        '  codex-evaluator:\n    agent: codex\n'
        '    args: ["--sandbox", "read-only"]\n', encoding="utf-8",
    )
    graph_rt = GraphRuntime(
        GraphRuntimeStore(runtime.db),
        repository_key=lambda n: str(repos[n]),
        readiness_guard=requests.assert_graph_ready,
    )
    nodes = [{
        "node_id": "backend", "repository": "backend",
        "route": "codex-balanced",
        "task_packet": {
            "objective": "Implement total", "scope": ["orders.py"],
            "acceptance_criteria": ["total doubles input"],
        },
    }]
    if multi:
        nodes.append({
            "node_id": "frontend", "repository": "frontend",
            "route": "codex-balanced", "depends_on": ["backend"],
            "task_packet": {
                "objective": "Display total", "scope": ["client.py"],
                "acceptance_criteria": ["Format backend total"],
            },
        })
    graph = graph_rt.start_graph(
        task_graph_from_payload({"nodes": nodes}),
        repository_names=repos.keys(),
    )
    gid = graph["graph_run_id"]
    if with_request:
        request = requests.create("Display prices correctly end to end")
        rid = request["request_id"]
        assessment = requests.assess(rid, 1, {
            "requirements": [
                {"id": "R1", "text": "Correct total and display",
                 "evidence_refs": ["request:current"]},
            ],
            "blocking_unknowns": [], "decision": "direct",
            "rationale": "Specified contract",
        })
        requests.bind_graph(
            gid, rid, assessment["revision"], [n["node_id"] for n in nodes],
            {n["node_id"]: ["R1"] for n in nodes},
        )
    with sqlite3.connect(runtime.db) as db:
        for node in nodes:
            db.execute(
                "UPDATE graph_node_states SET semantic_state='satisfied', "
                "runtime_state='idle', turn_id=? WHERE graph_run_id=? AND node_id=?",
                ("turn-" + node["node_id"], gid, node["node_id"]),
            )
    return runtime, requests, graph_rt, FinalEvaluationCoordinator(
        runtime, graph_rt, requests,
    ), gid


def evidence(manifest, repo):
    item = next(f for f in manifest[repo]["files"]
                if f["path"].endswith(".py"))
    return {"repository": repo, "path": item["path"],
            "sha256": item["sha256"], "locator": item["path"] + ":1"}


def report(manifest, *, valid=True, verdict="pass"):
    return {
        "verdict": verdict,
        "requirement_results": [{
            "requirement_id": "R1",
            "status": "pass" if valid else "inconclusive",
            "evidence": [evidence(manifest, "backend")],
            "rationale": "Inspected backend implementation",
        }],
        "cross_repository_checks": ([{
            "name": "Backend/Frontend contract",
            "status": "pass",
            "evidence": [evidence(manifest, "backend"),
                         evidence(manifest, "frontend")],
            "rationale": "Traced consumer and producer",
        }] if "frontend" in manifest else []),
        "verification_runs": [],
        "findings": [],
        "unknowns": [] if valid else ["Need integration verification"],
    }


def test_one_fresh_cross_repo_evaluation_and_finalize(tmp_path, monkeypatch):
    runtime, requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    seen = []
    original_backend = (runtime.repos()["backend"] / "orders.py").read_bytes()

    async def fake_delegate(**kwargs):
        seen.append(kwargs)
        assert kwargs["evaluation_repositories"] == ("backend", "frontend")
        assert kwargs["evaluation_roots"]["backend"] != runtime.repos()["backend"]
        assert kwargs["evaluation_roots"]["frontend"] != runtime.repos()["frontend"]
        for name, path in kwargs["evaluation_roots"].items():
            assert path.is_dir()
            assert (path / ".qiqi-evaluation-manifest.json").is_file()
        assert kwargs["session_id"] if "session_id" in kwargs else True
        coordinator.store.bind_turn(kwargs["evaluation_id"], "final-native-turn")
        capture = coordinator.store.get(kwargs["evaluation_id"])
        return {"state": "settled", "turn_id": "final-native-turn",
                "agent_response": json.dumps(report(capture["manifest"]))}

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    revision = graph_rt.get_graph(gid)["revision"]
    outcome = asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    assert outcome["status"] == "passed"
    assert len(seen) == 1
    assert outcome["is_current"] is True
    assert (runtime.repos()["backend"] / "orders.py").read_bytes() == original_backend
    assert not seen[0]["evaluation_roots"]["backend"].exists()
    assert coordinator.graph_status(gid)["delivery_status"] == "not_finalized"

    completed = coordinator.finalize(gid, outcome["evaluation_id"], revision)
    assert completed["finalized_at_ns"]
    assert coordinator.graph_status(gid)["delivery_status"] == "finalized"
    with pytest.raises((RuntimeError, ValueError), match="already|current|finalized"):
        coordinator.finalize(gid, outcome["evaluation_id"], revision)


def test_snapshots_include_untracked_work_and_stale_pass_cannot_finalize(tmp_path, monkeypatch):
    runtime, _reqs, graph_rt, coordinator, gid = setup_graph(tmp_path)
    extra = runtime.repos()["frontend"] / "draft_untracked.py"
    extra.write_text("CONTRACT = 'v1'\n")
    async def fake_delegate(**kwargs):
        coordinator.store.bind_turn(kwargs["evaluation_id"], "captured-native")
        state = coordinator.store.get(kwargs["evaluation_id"])
        assert any(f["path"] == "draft_untracked.py"
                   for f in state["manifest"]["frontend"]["files"])
        return {"state": "settled", "turn_id": "captured-native",
                "agent_response": json.dumps(report(state["manifest"]))}
    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    revision = graph_rt.get_graph(gid)["revision"]
    outcome = asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    assert outcome["status"] == "passed"
    extra.write_text("CONTRACT = 'v2'\n")
    assert coordinator.read(gid)["effective_status"] == "stale"
    with pytest.raises((ValueError, RuntimeError), match="PASS"):
        coordinator.finalize(gid, outcome["evaluation_id"], revision)


def test_unsafe_route_and_unbound_graph_cannot_start(tmp_path):
    _runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    revision = graph_rt.get_graph(gid)["revision"]
    with pytest.raises(ValueError, match="requires Codex route"):
        asyncio.run(coordinator.start(gid, "codex-balanced", revision))
    assert coordinator.store.latest(gid) is None
    _, _, graph_rt2, coordinator2, gid2 = setup_graph(
        tmp_path / "other", with_request=False,
    )
    with pytest.raises(ValueError, match="ineligible_missing_user_intent"):
        asyncio.run(coordinator2.start(
            gid2, "codex-evaluator", graph_rt2.get_graph(gid2)["revision"],
        ))


def test_invalid_report_is_inconclusive_and_cannot_finalize(tmp_path, monkeypatch):
    runtime, _reqs, graph_rt, coordinator, gid = setup_graph(tmp_path, multi=False)
    async def fake_delegate(**kwargs):
        coordinator.store.bind_turn(kwargs["evaluation_id"], "broken-native")
        return {"state": "settled", "turn_id": "broken-native",
                "agent_response": json.dumps({"verdict": "pass"})}
    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    revision = graph_rt.get_graph(gid)["revision"]
    outcome = asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    assert outcome["status"] == "inconclusive"
    assert "Invalid FinalEvaluationReport" in outcome["detail"]
    assert outcome["raw_response"]
    with pytest.raises(ValueError, match="PASS"):
        coordinator.finalize(gid, outcome["evaluation_id"], revision)


def test_pass_requires_manifest_evidence_and_complete_original_coverage(tmp_path):
    runtime, _reqs, graph_rt, coordinator, _gid = setup_graph(tmp_path)
    manifest = inspect_roots(runtime.repos())
    validated = coordinator.store.validate_report(report(manifest), manifest, {"R1"})
    assert validated["verdict"] == "pass"
    bad = report(manifest)
    bad["requirement_results"][0]["evidence"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="manifest"):
        coordinator.store.validate_report(bad, manifest, {"R1"})
    with pytest.raises(ValueError, match="all original requirements"):
        coordinator.store.validate_report(report(manifest), manifest, {"R1", "R2"})
    conflict = report(manifest)
    conflict["findings"] = [{
        "id": "EVAL-1", "severity": "blocking",
        "repositories": ["backend"], "affected_requirements": ["R1"],
        "evidence": [evidence(manifest, "backend")],
        "proposed_follow_up": "Fix compatibility",
    }]
    with pytest.raises(ValueError, match="claimed PASS"):
        coordinator.store.validate_report(conflict, manifest, {"R1"})


def test_snapshot_rejects_symlinks_and_sensitive_files(tmp_path):
    runtime, _reqs, _graph_rt, _coord, _gid = setup_graph(tmp_path)
    root = runtime.repos()["backend"]
    (root / "malicious").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink"):
        with EvaluationSnapshot({"backend": root}):
            pass
    (root / "malicious").unlink()
    (root / ".env").write_text("SECRET_KEY=hidden\n")
    # Untracked .env may be ignored in some project layouts; test explicit
    # tracked secret to prove snapshot cannot silently omit it.
    subprocess = __import__("subprocess")
    subprocess.run(["git", "-C", str(root), "add", "-f", ".env"], check=True)
    with pytest.raises(ValueError, match="sensitive"):
        with EvaluationSnapshot({"backend": root}):
            pass


def test_snapshot_drift_includes_dirty_and_untracked_changes(tmp_path):
    runtime, _reqs, _graph_rt, _coord, _gid = setup_graph(tmp_path)
    roots = runtime.repos()
    first = manifest_digest(inspect_roots(roots))
    file = roots["backend"] / "orders.py"
    file.write_text(file.read_text() + "\n# uncommitted\n")
    second = manifest_digest(inspect_roots(roots))
    assert first != second
    (roots["backend"] / "new_contract.py").write_text("VERSION = 2\n")
    third = manifest_digest(inspect_roots(roots))
    assert second != third
