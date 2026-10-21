"""Final Evaluation Gate end-to-end behavior with isolated fake native Evaluator."""
import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest

from test_task_readiness import workspace
from qiqi_delegate.final_eval import FinalEvaluationCoordinator
from qiqi_delegate.evaluator_routes import CLAUDE_EVALUATOR_ARGS, validate_evaluator_route
from qiqi_delegate.final_eval_manifest import inspect_roots, manifest_digest
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
        '    args: ["--sandbox", "read-only"]\n'
        '  claude-evaluator:\n    agent: claude\n'
        f'    args: {json.dumps(list(CLAUDE_EVALUATOR_ARGS))}\n'
        '  claude-unsafe:\n    agent: claude\n'
        '    args: ["--permission-mode", "plan"]\n', encoding="utf-8",
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



@pytest.mark.parametrize("route", ["codex-evaluator", "claude-evaluator"])
def test_one_fresh_cross_repo_evaluation_and_finalize(tmp_path, monkeypatch, route):
    runtime, requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    seen = []
    original_backend = (runtime.repos()["backend"] / "orders.py").read_bytes()

    async def fake_delegate(**kwargs):
        seen.append(kwargs)
        assert kwargs["evaluation_repositories"] == ("backend", "frontend")
        assert kwargs["evaluation_roots"]["backend"] == runtime.repos()["backend"]
        assert kwargs["evaluation_roots"]["frontend"] == runtime.repos()["frontend"]
        for name, path in kwargs["evaluation_roots"].items():
            assert path.is_dir()
            assert not (path / ".qiqi-evaluation-manifest.json").exists()
        assert kwargs["session_id"] if "session_id" in kwargs else True
        coordinator.store.bind_turn(kwargs["evaluation_id"], "final-native-turn")
        capture = coordinator.store.get(kwargs["evaluation_id"])
        body = json.dumps(report(capture["manifest"]))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("final-native-turn", "native-evaluator", "backend",
                 route, "settled", body, 123),
            )
        return {"state": "settled", "turn_id": "final-native-turn",
                "agent_response": body}

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    revision = graph_rt.get_graph(gid)["revision"]
    outcome = asyncio.run(coordinator.start(gid, route, revision))
    assert outcome["status"] == "passed"
    assert len(seen) == 1
    assert outcome["is_current"] is True
    assert (runtime.repos()["backend"] / "orders.py").read_bytes() == original_backend
    assert seen[0]["evaluation_roots"]["backend"].is_dir()
    assert coordinator.graph_status(gid)["delivery_status"] == "not_finalized"

    completed = coordinator.finalize(gid, outcome["evaluation_id"], revision)
    assert completed["finalized_at_ns"]
    assert coordinator.graph_status(gid)["delivery_status"] == "finalized"
    with pytest.raises((RuntimeError, ValueError), match="already|current|finalized"):
        coordinator.finalize(gid, outcome["evaluation_id"], revision)



def test_live_manifest_includes_untracked_work_and_stale_pass_cannot_finalize(tmp_path, monkeypatch):
    runtime, _reqs, graph_rt, coordinator, gid = setup_graph(tmp_path)
    extra = runtime.repos()["frontend"] / "draft_untracked.py"
    extra.write_text("CONTRACT = 'v1'\n")
    async def fake_delegate(**kwargs):
        coordinator.store.bind_turn(kwargs["evaluation_id"], "captured-native")
        state = coordinator.store.get(kwargs["evaluation_id"])
        assert any(f["path"] == "draft_untracked.py"
                   for f in state["manifest"]["frontend"]["files"])
        body = json.dumps(report(state["manifest"]))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("captured-native", "native-evaluator", "backend",
                 "codex-evaluator", "settled", body, 124),
            )
        return {"state": "settled", "turn_id": "captured-native",
                "agent_response": body}
    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    revision = graph_rt.get_graph(gid)["revision"]
    outcome = asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    assert outcome["status"] == "passed"
    extra.write_text("CONTRACT = 'v2'\n")
    assert coordinator.read(gid)["effective_status"] == "stale"
    with pytest.raises((ValueError, RuntimeError), match="PASS"):
        coordinator.finalize(gid, outcome["evaluation_id"], revision)



def test_user_configured_evaluator_route_and_unbound_graph_gate(tmp_path, monkeypatch):
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    revision = graph_rt.get_graph(gid)["revision"]

    async def fake_delegate(**kwargs):
        assert kwargs["route"] == "codex-balanced"
        raise RuntimeError("custom CLI route dispatched")

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    # User-selected permissions are no longer a profile validation error.
    with pytest.raises(RuntimeError, match="custom CLI route dispatched"):
        asyncio.run(coordinator.start(gid, "codex-balanced", revision))
    assert coordinator.store.latest(gid) is not None

    # TaskGraph binding/readiness gate is still mandatory with any CLI profile.
    other = tmp_path / "other"
    other.mkdir()
    _, _, graph_rt2, coordinator2, gid2 = setup_graph(
        other, with_request=False,
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
    assert "FinalEvaluationReport" in outcome["detail"]
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



def test_abandoned_native_capture_never_restores_pass_before_validation(tmp_path):
    from qiqi_delegate.final_eval_store import FinalEvaluationStore
    from qiqi_delegate.maintenance import recover_final_evaluation

    runtime, requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    graph = graph_rt.get_graph(gid)
    binding = requests.graph_binding(gid)
    manifest = inspect_roots(runtime.repos())
    reserved, created = coordinator.store.reserve(
        gid, graph["revision"], binding["request_id"],
        binding["request_revision"], "codex-evaluator",
        manifest, manifest_digest(manifest),
    )
    assert created
    evaluation_id = reserved["evaluation_id"]
    coordinator.store.bind_turn(evaluation_id, "recovery-capture")
    body = json.dumps(report(manifest))
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("recovery-capture", "native-session", "backend",
             "codex-evaluator", "settled", body, 123),
        )
        db.execute(
            "UPDATE final_evaluations SET owner_pid=?, owner_start_token=? "
            "WHERE evaluation_id=?", (987654321, "dead-process", evaluation_id),
        )
    # Simulate process death after native capture persistence but before
    # Coordinator validation. A captured response alone cannot authorize PASS.
    restarted = FinalEvaluationStore(runtime.db)
    restored = restarted.get(evaluation_id)
    assert restored["status"] == "interrupted"
    assert restored["turn_id"] == "recovery-capture"
    assert restored["raw_response"] == body
    assert restored["native_capture"]["response"] == body
    assert restored["report"] is None
    assert "coordinator verification" in restored["detail"]
    assert restarted.recover_abandoned() == 0
    with pytest.raises(ValueError, match="PASS"):
        coordinator.finalize(gid, evaluation_id, graph["revision"])
    with pytest.raises(RuntimeError, match="operator"):
        restarted.reserve(
            gid, graph["revision"], binding["request_id"],
            binding["request_revision"], "codex-evaluator",
            manifest, manifest_digest(manifest),
        )
    cleared = recover_final_evaluation(
        workspace=runtime.root, evaluation_id=evaluation_id,
        worker_termination_confirmed=True,
    )
    assert cleared["status"] == "errored"
    assert cleared["report"] is None



def test_abandoned_capture_with_held_claim_cannot_authorize_pass(tmp_path):
    from qiqi_delegate.final_eval_store import FinalEvaluationStore

    runtime, requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    binding = requests.graph_binding(gid)
    manifest = inspect_roots(runtime.repos())
    reserved, _ = coordinator.store.reserve(
        gid, graph_rt.get_graph(gid)["revision"], binding["request_id"],
        binding["request_revision"], "codex-evaluator",
        manifest, manifest_digest(manifest),
    )
    eid = reserved["evaluation_id"]
    turn_id = "held-claim-capture"
    coordinator.store.bind_turn(eid, turn_id)
    native = json.dumps(report(manifest))
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?,?,?,?,?,?,?)",
            (turn_id, "native-session", "backend", "codex-evaluator",
             "settled", native, 123),
        )
        db.execute(
            "INSERT INTO write_claims(repository,claim_id,created_at_ns,repository_root) "
            "VALUES (?,?,?,?)",
            ("backend", "turn:" + turn_id, 123, str(runtime.repos()["backend"])),
        )
        db.execute(
            "UPDATE final_evaluations SET owner_pid=?, owner_start_token=? "
            "WHERE evaluation_id=?", (987654321, "dead-process", eid),
        )
    row = FinalEvaluationStore(runtime.db).get(eid)
    assert row["status"] == "interrupted"
    assert row["report"] is None
    assert row["raw_response"] == native
    assert "write claim" in row["detail"]
    with pytest.raises(ValueError, match="PASS"):
        coordinator.finalize(gid, eid, graph_rt.get_graph(gid)["revision"])



def test_unproven_old_worker_requires_operator_clearance(tmp_path):
    from qiqi_delegate.final_eval_store import FinalEvaluationStore
    from qiqi_delegate.maintenance import recover_final_evaluation
    runtime, requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    binding = requests.graph_binding(gid)
    rev = graph_rt.get_graph(gid)["revision"]
    manifest = inspect_roots(runtime.repos())
    created, _ = coordinator.store.reserve(
        gid, rev, binding["request_id"], binding["request_revision"],
        "codex-evaluator", manifest, manifest_digest(manifest),
    )
    eid = created["evaluation_id"]
    coordinator.store.bind_turn(eid, "no-native-capture")
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "UPDATE final_evaluations SET owner_pid=?, owner_start_token=? "
            "WHERE evaluation_id=?", (987654321, "dead-process", eid),
        )
    fresh = FinalEvaluationStore(runtime.db)
    assert fresh.get(eid)["status"] == "interrupted"
    with pytest.raises(RuntimeError, match="operator"):
        fresh.reserve(
            gid, rev, binding["request_id"], binding["request_revision"],
            "codex-evaluator", manifest, manifest_digest(manifest),
        )
    with pytest.raises(ValueError, match="termination"):
        recover_final_evaluation(
            workspace=runtime.root, evaluation_id=eid,
            worker_termination_confirmed=False,
        )
    recovered = recover_final_evaluation(
        workspace=runtime.root, evaluation_id=eid,
        worker_termination_confirmed=True,
    )
    assert recovered["status"] == "errored"
    new, actually_created = fresh.reserve(
        gid, rev, binding["request_id"], binding["request_revision"],
        "codex-evaluator", manifest, manifest_digest(manifest),
    )
    assert actually_created and new["evaluation_id"] != eid



def test_duplicate_active_evaluation_does_not_dispatch_again(tmp_path, monkeypatch):
    runtime, _reqs, graph_rt, coordinator, gid = setup_graph(tmp_path)
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def fake_delegate(**kwargs):
        calls.append(kwargs)
        coordinator.store.bind_turn(kwargs["evaluation_id"], "once-native")
        started.set()
        await finish.wait()
        state = coordinator.store.get(kwargs["evaluation_id"])
        body = json.dumps(report(state["manifest"]))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("once-native", "native-evaluator", "backend",
                 "codex-evaluator", "settled", body, 333),
            )
        return {"state": "settled", "turn_id": "once-native",
                "agent_response": body}

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    async def run():
        revision = graph_rt.get_graph(gid)["revision"]
        first = asyncio.create_task(
            coordinator.start(gid, "codex-evaluator", revision),
        )
        await started.wait()
        second = await coordinator.start(gid, "codex-evaluator", revision)
        assert second["already_active"] is True
        assert second["status"] == "evaluating"
        finish.set()
        return await first, second
    first, second = asyncio.run(run())
    assert len(calls) == 1
    assert first["status"] == "passed"
    assert first["evaluation_id"] == second["evaluation_id"]




@pytest.mark.parametrize("adapter,args", [
    ("codex", ["--yolo"]),
    ("claude", ["--permission-mode", "plan"]),
    ("claude", ["--model", "claude-sonnet-5-5", "--effort", "xhigh",
                "--permission-mode", "auto"]),
    ("claude", ["--dangerously-skip-permissions"]),
    ("claude", ["--strict-mcp-config", "--tools", "Read,Glob,Grep"]),
    ("codex", ["--yolo"]),
])
def test_evaluator_route_profiles_accept_user_cli_args(adapter, args):
    # Compatibility function no longer mandates exact evaluator permissions.
    validate_evaluator_route(adapter, args)


@pytest.mark.parametrize("args, expected", [
    (["--system-prompt-file", "./rules.txt"],
     ["--system-prompt-file", "rules.txt"]),
    (["--append-system-prompt-file=./extra.txt"],
     ["--append-system-prompt-file=extra.txt"]),
    (["--system-prompt-file", "../shared/guide.txt",
      "--append-system-prompt-file", "./extra.txt"],
     ["--system-prompt-file", "../shared/guide.txt",
      "--append-system-prompt-file", "extra.txt"]),
    (["--add-dir", "./reference-a", "../reference-b", "--model", "sonnet"],
     ["--add-dir", "reference-a", "../reference-b", "--model", "sonnet"]),
    (["--add-dir=./reference-a", "./reference-b", "--effort", "high"],
     ["--add-dir=reference-a", "reference-b", "--effort", "high"]),
    (["--plugin-dir", "./plugins", "--plugin-dir=./plugin.zip"],
     ["--plugin-dir", "plugins", "--plugin-dir=./plugin.zip"]),
    (["--debug-file", "./logs/debug.log", "--debug-file=../trace.log"],
     ["--debug-file", "logs/debug.log", "--debug-file=../trace.log"]),
])
def test_claude_evaluator_rebases_path_operands_against_routing_workspace(
    tmp_path, args, expected,
):
    from qiqi_delegate.runtime import _resolve_evaluator_claude_path_args

    workspace_root = tmp_path / "control"
    workspace_root.mkdir()
    original_args = list(args)
    actual = _resolve_evaluator_claude_path_args(args, workspace_root)
    for original, normalized in zip(expected, actual):
        if original.startswith("--") and "=" in original:
            option, relative = original.split("=", 1)
            assert normalized == f"{option}={(workspace_root / relative).resolve()}"
        elif original.startswith("--"):
            assert normalized == original
        elif original in {"sonnet", "high"}:
            assert normalized == original
        else:
            assert normalized == str((workspace_root / original).resolve())
    assert args == original_args



def test_claude_evaluator_keeps_absolute_paths_unchanged(tmp_path):
    from qiqi_delegate.runtime import _resolve_evaluator_claude_path_args

    absolute = str((tmp_path / "shared prompt.txt").resolve())
    args = ["--system-prompt-file", absolute,
            f"--append-system-prompt-file={absolute}",
            "--add-dir", absolute, "--plugin-dir", absolute,
            f"--debug-file={absolute}"]
    assert _resolve_evaluator_claude_path_args(
        args, tmp_path / "control",
    ) == args


def test_evaluator_cwd_is_stable_and_outside_candidate_roots(tmp_path, monkeypatch):
    from qiqi_delegate.runtime import evaluator_working_directory

    repos = {"backend": tmp_path / "backend"}
    snapshot = {"backend": tmp_path / "snapshot"}
    repos["backend"].mkdir()
    snapshot["backend"].mkdir()
    trusted = tmp_path / "trusted" / "cwd"
    monkeypatch.setenv("QIQI_EVALUATOR_CWD", str(trusted))
    first = evaluator_working_directory(repos, snapshot)
    assert first == trusted.resolve()
    assert evaluator_working_directory(repos, snapshot) == first
    assert first.is_dir()

    monkeypatch.setenv("QIQI_EVALUATOR_CWD", str(repos["backend"] / "nested"))
    with pytest.raises(ValueError, match="outside registered"):
        evaluator_working_directory(repos, snapshot)
    monkeypatch.setenv("QIQI_EVALUATOR_CWD", str(tmp_path))
    with pytest.raises(ValueError, match="outside registered"):
        evaluator_working_directory(repos, snapshot)
    monkeypatch.setenv("QIQI_EVALUATOR_CWD", "relative/evaluator")
    with pytest.raises(ValueError, match="absolute path"):
        evaluator_working_directory(repos, snapshot)
    monkeypatch.setenv("QIQI_EVALUATOR_CWD", "")
    with pytest.raises(ValueError, match="must not be empty"):
        evaluator_working_directory(repos, snapshot)



def test_per_turn_native_cwd_does_not_inherit_previous_run_artifacts(tmp_path, monkeypatch):
    import uuid
    from qiqi_delegate.runtime import evaluator_run_directory, release_evaluator_run_lock
    from qiqi_delegate.runtime import cleanup_temporary_tree

    root = tmp_path / "trusted"
    monkeypatch.setenv("QIQI_EVALUATOR_CWD", str(root))
    source = tmp_path / "repo"
    source.mkdir()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    repos = {"repo": source}
    snapshots = {"repo": snapshot}
    first = evaluator_run_directory(repos, snapshots, str(uuid.uuid4()))
    (first / "CLAUDE.md").write_text("untrusted first-turn content")
    (first / "AGENTS.md").write_text("untrusted first-turn content")
    with pytest.raises(RuntimeError, match="another evaluator is active"):
        evaluator_run_directory(repos, snapshots, str(uuid.uuid4()))
    cleanup_temporary_tree(first)
    release_evaluator_run_lock(first)
    second = evaluator_run_directory(repos, snapshots, str(uuid.uuid4()))
    assert first != second and first.parent == second.parent == root / "runs"
    assert list(second.iterdir()) == []
    cleanup_temporary_tree(second)
    release_evaluator_run_lock(second)
    assert root.is_dir()



def test_evaluator_refuses_poisoned_trusted_ancestors(tmp_path, monkeypatch):
    import uuid
    from qiqi_delegate.runtime import evaluator_run_directory

    root = tmp_path / "trusted"
    root.mkdir()
    monkeypatch.setenv("QIQI_EVALUATOR_CWD", str(root))
    repo = tmp_path / "repo"
    repo.mkdir()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (root / "CLAUDE.md").write_text("poisoned")
    with pytest.raises(ValueError, match="ancestor contains startup instructions"):
        evaluator_run_directory({"repo": repo}, {"repo": snapshot}, str(uuid.uuid4()))
    (root / "CLAUDE.md").unlink()
    (root / "runs").mkdir()
    (root / "runs" / "AGENTS.md").write_text("poisoned")
    with pytest.raises(ValueError, match="unexpected files"):
        evaluator_run_directory({"repo": repo}, {"repo": snapshot}, str(uuid.uuid4()))



def test_recovery_refuses_arbitrary_evaluator_cwd(tmp_path, monkeypatch):
    from qiqi_delegate.maintenance import _cleanup_retained_evaluation_paths

    root = tmp_path / "trusted"
    monkeypatch.setenv("QIQI_EVALUATOR_CWD", str(root))
    victim = tmp_path / "other" / ("turn-" + "b" * 32)
    victim.mkdir(parents=True)
    (victim / "keep.txt").write_text("must not delete")
    with pytest.raises(ValueError, match="unsafe retained evaluator CWD"):
        _cleanup_retained_evaluation_paths({"evaluator_cwd": str(victim)})
    assert (victim / "keep.txt").is_file()



def test_evaluator_run_lease_serializes_while_first_worker_is_active(
    tmp_path, monkeypatch,
):
    import uuid
    from qiqi_delegate.runtime import (
        evaluator_run_directory, release_evaluator_run_lock,
    )
    from qiqi_delegate.runtime import cleanup_temporary_tree

    trusted = tmp_path / "trusted"
    monkeypatch.setenv("QIQI_EVALUATOR_CWD", str(trusted))
    repo = tmp_path / "repo"
    snapshot = tmp_path / "snapshot"
    repo.mkdir()
    snapshot.mkdir()
    repos, snapshots = {"repo": repo}, {"repo": snapshot}

    first = evaluator_run_directory(repos, snapshots, str(uuid.uuid4()))
    lease = trusted / "runs" / ".qiqi-active"
    assert lease.read_text().strip() == first.name
    with pytest.raises(RuntimeError, match="another evaluator is active"):
        evaluator_run_directory(repos, snapshots, str(uuid.uuid4()))
    assert first.is_dir() and lease.is_file()
    cleanup_temporary_tree(first)
    release_evaluator_run_lock(first)
    second = evaluator_run_directory(repos, snapshots, str(uuid.uuid4()))
    assert second != first
    assert list(second.iterdir()) == []
    cleanup_temporary_tree(second)
    release_evaluator_run_lock(second)
    assert {item.name for item in (trusted / "runs").iterdir()} == {".qiqi-mutex"}



def test_evaluator_checks_all_ancestor_instructions(tmp_path, monkeypatch):
    import uuid
    from qiqi_delegate.runtime import evaluator_run_directory

    ancestor = tmp_path / "trusted-parent"
    ancestor.mkdir()
    trusted = ancestor / "nested" / "evaluator-cwd"
    monkeypatch.setenv("QIQI_EVALUATOR_CWD", str(trusted))
    repo, snapshot = tmp_path / "repo", tmp_path / "snapshot"
    repo.mkdir()
    snapshot.mkdir()
    malicious = ancestor / "CLAUDE.md"
    malicious.write_text("injected instructions")
    with pytest.raises(ValueError, match="ancestor contains startup instructions"):
        evaluator_run_directory(
            {"repo": repo}, {"repo": snapshot}, str(uuid.uuid4()),
        )
    malicious.unlink()
    # The same bound applies to deeper custom path ancestors, not just the
    # trust root itself.
    ancestor_settings = ancestor / ".claude"
    ancestor_settings.mkdir()
    with pytest.raises(ValueError, match="ancestor contains project settings"):
        evaluator_run_directory(
            {"repo": repo}, {"repo": snapshot}, str(uuid.uuid4()),
        )



def test_evaluator_rejects_injected_runs_project_settings_after_previous_turn(
    tmp_path, monkeypatch,
):
    import uuid
    from qiqi_delegate.runtime import (
        evaluator_run_directory, release_evaluator_run_lock,
    )
    from qiqi_delegate.runtime import cleanup_temporary_tree

    trust_root = tmp_path / "trusted"
    monkeypatch.setenv("QIQI_EVALUATOR_CWD", str(trust_root))
    repository = tmp_path / "repository"
    snapshot = tmp_path / "snapshot"
    repository.mkdir()
    snapshot.mkdir()
    repos, snapshots = {"backend": repository}, {"backend": snapshot}
    first = evaluator_run_directory(repos, snapshots, str(uuid.uuid4()))
    runs = trust_root / "runs"
    assert first.is_dir()
    cleanup_temporary_tree(first)
    release_evaluator_run_lock(first)

    # The first evaluator can write to its parent under the same OS UID.
    # Startup must fail closed rather than load those persistent settings.
    settings = runs / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"hooks": {"Stop": []}}', encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected files"):
        evaluator_run_directory(repos, snapshots, str(uuid.uuid4()))
    assert not (runs / ".qiqi-active").exists()
    cleanup_temporary_tree(settings.parent)

    # No miscellaneous sibling directories are permitted either.
    (runs / "untrusted-artifacts").mkdir()
    with pytest.raises(ValueError, match="unexpected files"):
        evaluator_run_directory(repos, snapshots, str(uuid.uuid4()))
    cleanup_temporary_tree(runs / "untrusted-artifacts")

    # A valid retained turn-* directory is allowed: successful startup
    # creates a fresh distinct CWD with no inherited project settings.
    second = evaluator_run_directory(repos, snapshots, str(uuid.uuid4()))
    assert first != second
    assert list(second.iterdir()) == []
    cleanup_temporary_tree(second)
    release_evaluator_run_lock(second)



def test_direct_root_manifest_supports_files_over_old_copy_limit(tmp_path):
    runtime, _requests, _graph_rt, _coordinator, _gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    large = backend / "large_contract.txt"
    large.write_bytes(b"x" * 1_400_000)
    manifest = inspect_roots(runtime.repos())
    record = next(x for x in manifest["backend"]["files"]
                  if x["path"] == "large_contract.txt")
    import hashlib
    assert record["sha256"] == hashlib.sha256(large.read_bytes()).hexdigest()
    assert record["size"] == 1_400_000
    assert not any(p.name.startswith("qiqi-final-eval-")
                   for p in tmp_path.iterdir())


def test_multi_root_claims_are_atomic_and_block_concurrent_peer(tmp_path):
    runtime, _requests, _graph_rt, _coordinator, _gid = setup_graph(tmp_path)
    roots = ("backend", "frontend")
    runtime._claim_many(roots, "turn:evaluation-one")
    try:
        with pytest.raises(RuntimeError, match="busy"):
            runtime._claim(
                "frontend", "turn:peer-write",
                repository_root=runtime.repos()["frontend"],
            )
        with pytest.raises(RuntimeError, match="busy"):
            runtime._claim_many(roots, "turn:evaluation-two")
        # A failed multi-root reservation must not leave partial claims.
        with sqlite3.connect(runtime.db) as db:
            assert db.execute(
                "SELECT COUNT(*) FROM write_claims WHERE claim_id=?",
                ("turn:evaluation-two",),
            ).fetchone()[0] == 0
    finally:
        runtime._release_claims(roots, "turn:evaluation-one")
    with sqlite3.connect(runtime.db) as db:
        assert db.execute("SELECT COUNT(*) FROM write_claims").fetchone()[0] == 0


def test_original_graph_evidence_is_provided_as_untrusted_context(tmp_path):
    runtime, requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    graph, authored, binding, task, names, roots = coordinator._eligible(gid)
    manifest = inspect_roots(roots)
    context = coordinator._context(
        gid, graph, authored, binding, task, names,
        manifest, manifest_digest(manifest),
    )
    assert context["original_user_request"] == task["user_request"]
    assert {r["id"] for r in context["requirements"]} == {"R1"}
    assert {n["node_id"] for n in context["task_graph"]} == {
        "backend", "frontend",
    }
    assert {n["node_id"] for n in context["node_evidence"]} == {
        "backend", "frontend",
    }
    assert "Lead ACCEPT" in context["node_evidence"][0]["warning"]


def test_direct_source_mutation_during_review_never_passes(tmp_path, monkeypatch):
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path)

    async def fake_delegate(**kwargs):
        coordinator.store.bind_turn(kwargs["evaluation_id"], "mutating-turn")
        manifest = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        body = json.dumps(report(manifest))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?,?,?,?,?,?,?)",
                ("mutating-turn", "native-session", "backend",
                 "codex-evaluator", "settled", body, 123),
            )
        (runtime.repos()["frontend"] / "client.py").write_text(
            "def show(total):\n    return 'changed' + str(total)\n"
        )
        return {"state": "settled", "turn_id": "mutating-turn",
                "agent_response": body}

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    revision = graph_rt.get_graph(gid)["revision"]
    result = asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    assert result["status"] == "inconclusive"
    assert "changed" in result["detail"]
    assert not result["is_current"]
    assert coordinator.graph_status(gid)["delivery_status"] == "not_finalized"


def test_legacy_policy_one_final_pass_is_not_reused(tmp_path, monkeypatch):
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path, multi=False)
    called = []

    async def fake_delegate(**kwargs):
        called.append(kwargs["evaluation_id"])
        coordinator.store.bind_turn(kwargs["evaluation_id"], "policy-native-" + str(len(called)))
        manifest = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        body = json.dumps(report(manifest))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?,?,?,?,?,?,?)",
                ("policy-native-" + str(len(called)), "native-session",
                 "backend", "codex-evaluator", "settled", body, 123),
            )
        return {"state": "settled", "turn_id": "policy-native-" + str(len(called)),
                "agent_response": body}

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    rev = graph_rt.get_graph(gid)["revision"]
    fresh = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    assert fresh["status"] == "passed"
    coordinator.finalize(gid, fresh["evaluation_id"], rev)
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "UPDATE final_evaluations SET policy_version=1 WHERE evaluation_id=?",
            (fresh["evaluation_id"],),
        )
    assert coordinator.graph_status(gid)["delivery_status"] == "not_finalized"
    renewed = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    assert renewed["status"] == "passed"
    assert renewed["evaluation_id"] != fresh["evaluation_id"]
    assert len(called) == 2


def test_same_root_module_contracts_do_not_replace_cross_root_checks(tmp_path):
    runtime, _requests, _graph_rt, coordinator, _gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    (backend / "services.py").write_text("def producer(): return 1\n")
    manifest = inspect_roots(runtime.repos())
    payload = report(manifest)
    backend_file = next(item for item in manifest["backend"]["files"]
                        if item["path"] == "services.py")
    payload["cross_repository_checks"].append({
        "name": "Backend orders-to-service contract",
        "status": "pass",
        "evidence": [
            evidence(manifest, "backend"),
            {"repository": "backend", "path": "services.py",
             "sha256": backend_file["sha256"], "locator": "services.py:1"},
        ],
        "rationale": "Verified the producer/consumer interface in one Git root",
    })
    assert coordinator.store.validate_report(payload, manifest, {"R1"})[
        "verdict"
    ] == "pass"
    payload["cross_repository_checks"] = payload["cross_repository_checks"][1:]
    with pytest.raises(ValueError, match="claimed PASS"):
        coordinator.store.validate_report(payload, manifest, {"R1"})


def test_ignored_env_secrets_are_rejected_before_native_evaluator(tmp_path, monkeypatch):
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    (backend / ".gitignore").write_text(".env\n", encoding="utf-8")
    (backend / ".env").write_text("API_TOKEN=private\n", encoding="utf-8")
    launched = []
    async def forbidden_delegate(**kwargs):
        launched.append(kwargs)
        raise AssertionError("native evaluator must not see ignored credentials")
    monkeypatch.setattr(runtime, "delegate", forbidden_delegate)
    with pytest.raises(ValueError, match="ignored sensitive path"):
        inspect_roots(runtime.repos())
    with pytest.raises(ValueError, match="ignored sensitive path"):
        asyncio.run(coordinator.start(
            gid, "codex-evaluator", graph_rt.get_graph(gid)["revision"],
        ))
    assert launched == []
    assert coordinator.store.latest(gid) is None


def test_multi_root_claim_uses_only_validated_registry_mapping(tmp_path, monkeypatch):
    runtime, _requests, _graph_rt, _coordinator, _gid = setup_graph(tmp_path)
    expected = runtime.repos()
    assert expected["backend"] != expected["frontend"]
    # Simulate an alias remap after the packet/--add-dir roots were fixed.
    monkeypatch.setattr(runtime, "repos", lambda: {
        "backend": expected["frontend"], "frontend": expected["backend"],
    })
    with pytest.raises(RuntimeError, match="mapping changed"):
        runtime._claim_many(
            ("backend", "frontend"), "turn:stale-root",
            expected_roots=expected,
        )
    with sqlite3.connect(runtime.db) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM write_claims WHERE claim_id=?",
            ("turn:stale-root",),
        ).fetchone()[0] == 0


def test_multi_root_claim_detects_mapping_change_inside_transaction(tmp_path, monkeypatch):
    runtime, _requests, _graph_rt, _coordinator, _gid = setup_graph(tmp_path)
    expected = runtime.repos()
    calls = []
    def changing_repos():
        calls.append(1)
        if len(calls) == 1:
            return expected
        return {"backend": expected["frontend"], "frontend": expected["backend"]}
    monkeypatch.setattr(runtime, "repos", changing_repos)
    with pytest.raises(RuntimeError, match="mapping changed"):
        runtime._claim_many(
            ("backend", "frontend"), "turn:transacted-root",
            expected_roots=expected,
        )
    with sqlite3.connect(runtime.db) as db:
        assert db.execute("SELECT COUNT(*) FROM write_claims").fetchone()[0] == 0
