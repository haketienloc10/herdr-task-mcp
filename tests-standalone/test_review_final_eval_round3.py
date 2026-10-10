"""Review #5479500455: full task evidence, preserved workers, gitlinks and finalize races."""
import asyncio
import json
import sqlite3
import subprocess

import pytest

from qiqi_delegate.final_eval_snapshot import EvaluationSnapshot, inspect_roots, manifest_digest
from qiqi_delegate.runtime import AgentStartupBlocked
from test_final_evaluation import setup_graph, report


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        capture_output=True, text=True,
    ).stdout


def _task_with_sources(requests, graph_runtime, gid, sources):
    binding = requests.graph_binding(gid)
    rid = binding["request_id"]
    revision = requests.get(rid)["revision"]
    for source in sources:
        current = requests.append(rid, revision, source)
        revision = current["revision"]
    refs = [s["id"] for s in current["sources"]]
    assessed = requests.assess(rid, revision, {
        "requirements": [{
            "id": "R1",
            "text": "Implement attached specification, not merely Lead summary",
            "evidence_refs": ["request:current", *refs],
        }],
        "blocking_unknowns": [],
        "decision": "direct",
        "rationale": "Full specification supplied as source",
    })
    requests.bind_graph(
        gid, rid, assessed["revision"],
        list(binding["requirement_map"]),
        binding["requirement_map"],
        replace=True,
    )
    return assessed


def test_final_evaluator_can_read_every_original_source_from_snapshot(tmp_path, monkeypatch):
    runtime, requests, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    roots = runtime.repos()
    workspace_file = runtime.root / "specification.md"
    workspace_file.write_text(
        "Workspace specification: use stable decimal prices.\n",
        encoding="utf-8",
    )
    native_capture = "Discovery verified cross-module behavior.\n" + "X" * 115000
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("discovery-attachment", "native-session", "backend",
             "codex-balanced", "settled", native_capture, 44),
        )
    assessed = _task_with_sources(
        requests, graph_runtime, gid, [
            {"kind": "inline", "text": "CRITICAL REQUIREMENT: display VAT-inclusive prices."},
            {"kind": "repo_file", "repository": "backend", "path": "orders.py"},
            {"kind": "workspace_file", "path": "specification.md"},
            {"kind": "peer_turn", "turn_id": "discovery-attachment"},
        ],
    )
    expected = {s["id"]: s["content"] for s in assessed["sources"]}
    observed = {}

    async def fake_delegate(**kwargs):
        path = kwargs["evaluation_roots"]["backend"] / ".qiqi-final-task-sources"
        archive = json.loads((path / "index.json").read_text(encoding="utf-8"))
        assert len(archive["sources"]) == 4
        assert set(archive["requirements"][0]["evidence_refs"]) == (
            {"request:current"} | set(expected)
        )
        assert archive["original_user_request"] == assessed["user_request"]
        for entry in archive["sources"]:
            body = (path / entry["path"]).read_text(encoding="utf-8")
            assert body == expected[entry["id"]]
            assert entry["id"] in archive["requirements"][0]["evidence_refs"]
            observed[entry["id"]] = body
        # Source contents are available as files; don't truncate a >100k
        # native capture to fit the 100k TaskPacket.
        assert any(len(x) > 100_000 for x in observed.values())
        prompt = kwargs["packet"].to_json()
        assert ".qiqi-final-task-sources/index.json" in prompt
        assert "READ EVERY referenced source" in prompt
        coordinator.store.bind_turn(kwargs["evaluation_id"], "source-reviewed-turn")
        manifest = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        body = json.dumps(report(manifest))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("source-reviewed-turn", "native-session", "backend",
                 "codex-evaluator", "settled", body, 42),
            )
        return {"state": "settled", "turn_id": "source-reviewed-turn",
                "agent_response": body}

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    revision = graph_runtime.get_graph(gid)["revision"]
    result = asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    assert result["status"] == "passed"
    assert set(observed) == set(expected)


def test_missing_and_tampered_task_sources_fail_closed(tmp_path):
    runtime, requests, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    _task_with_sources(requests, graph_runtime, gid, [
        {"kind": "inline", "text": "Full specification required"},
    ])
    task = requests.get(requests.graph_binding(gid)["request_id"])
    roots = runtime.repos()
    with EvaluationSnapshot(roots) as snap:
        current = task.copy()
        current["sources"] = [dict(s) for s in task["sources"]]
        current["sources"][0]["content"] = "tampered specification"
        with pytest.raises(ValueError, match="digest mismatch"):
            coordinator._materialize_task_sources(current, snap.paths["backend"])
    with EvaluationSnapshot(roots) as snap:
        current = task.copy()
        current["assessment"] = json.loads(json.dumps(task["assessment"]))
        current["assessment"]["requirements"][0]["evidence_refs"].append("source:unknown")
        with pytest.raises(ValueError, match="missing task sources"):
            coordinator._materialize_task_sources(current, snap.paths["backend"])


def test_preserved_agent_startup_is_interrupted_and_requires_operator(tmp_path, monkeypatch):
    runtime, requests, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    async def blocked_delegate(**kwargs):
        # Native launch already bound turn ID before discovering that the
        # startup UI blocked; runtime deliberately preserves its worker.
        coordinator.store.bind_turn(kwargs["evaluation_id"], "blocked-native-turn")
        raise AgentStartupBlocked(
            "codex", "pane-123", "operator approval required",
        )
    monkeypatch.setattr(runtime, "delegate", blocked_delegate)
    revision = graph_runtime.get_graph(gid)["revision"]
    with pytest.raises(AgentStartupBlocked):
        asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    first = coordinator.store.latest(gid)
    assert first["status"] == "interrupted"
    assert first["turn_id"] == "blocked-native-turn"
    with pytest.raises(RuntimeError, match="operator"):
        asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    coordinator.store.release_interrupted(
        first["evaluation_id"], worker_termination_confirmed=True,
    )
    # Once the operator confirms the external Herdr worker has stopped, a
    # new attempt can reserve safely.
    async def clean_delegate(**kwargs):
        coordinator.store.bind_turn(kwargs["evaluation_id"], "retry-native")
        manifest = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        body = json.dumps(report(manifest))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("retry-native", "native-session", "backend",
                 "codex-evaluator", "settled", body, 55),
            )
        return {"state": "settled", "turn_id": "retry-native",
                "agent_response": body}
    monkeypatch.setattr(runtime, "delegate", clean_delegate)
    assert asyncio.run(
        coordinator.start(gid, "codex-evaluator", revision)
    )["status"] == "passed"


def test_git_submodule_in_index_is_rejected_not_marked_deleted(tmp_path):
    runtime, _requests, _graph, _coordinator, _gid = setup_graph(tmp_path)
    root = runtime.repos()["backend"]
    _git(
        root, "update-index", "--add", "--cacheinfo",
        "160000," + "1" * 40 + ",third_party/library",
    )
    (root / "third_party/library").mkdir(parents=True)
    (root / "third_party/library/README.md").write_text("dirty submodule\n")
    with pytest.raises(ValueError, match="submodule/gitlink"):
        inspect_roots({"backend": root})
    with pytest.raises(ValueError, match="submodule/gitlink"):
        with EvaluationSnapshot({"backend": root}):
            pass


def test_staged_submodule_deletion_still_rejected_from_head(tmp_path):
    runtime, _requests, _graph, _coordinator, _gid = setup_graph(tmp_path)
    root = runtime.repos()["backend"]
    _git(
        root, "update-index", "--add", "--cacheinfo",
        "160000," + "1" * 40 + ",third_party/library",
    )
    _git(
        root, "-c", "user.name=Evaluation Tests",
        "-c", "user.email=eval@example.invalid",
        "commit", "-m", "Tracked submodule gitlink",
    )
    _git(root, "rm", "--cached", "third_party/library")
    # The index no longer references the submodule. Its HEAD gitlink must
    # still cause an explicit fail-closed error instead of a deletion tombstone.
    with pytest.raises(ValueError, match="submodule/gitlink from HEAD"):
        inspect_roots({"backend": root})


def test_finalization_revoked_if_source_changes_during_commit(tmp_path, monkeypatch):
    runtime, requests, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    binding = requests.graph_binding(gid)
    manifest = inspect_roots(runtime.repos())
    rev = graph_runtime.get_graph(gid)["revision"]
    reserved, created = coordinator.store.reserve(
        gid, rev, binding["request_id"], binding["request_revision"],
        "codex-evaluator", manifest, manifest_digest(manifest),
    )
    assert created
    eid = reserved["evaluation_id"]
    coordinator.store.bind_turn(eid, "finalization-race-turn")
    body = json.dumps(report(manifest))
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("finalization-race-turn", "native-session", "backend",
             "codex-evaluator", "settled", body, 99),
        )
    coordinator.store.complete(
        eid, raw_response=body, report=report(manifest),
        status="passed", turn_id="finalization-race-turn",
    )
    original_finalize = coordinator.store.finalize
    rootfile = runtime.repos()["backend"] / "orders.py"

    def mutate_after_commit(*args, **kwargs):
        persisted = original_finalize(*args, **kwargs)
        # Simulate a Peer/worktree writer changing the deliverable precisely
        # after the SQLite finalization was committed.
        rootfile.write_text(rootfile.read_text() + "\n# concurrent mutation\n")
        return persisted

    monkeypatch.setattr(coordinator.store, "finalize", mutate_after_commit)
    with pytest.raises(RuntimeError, match="delivery was revoked"):
        coordinator.finalize(gid, eid, rev)
    row = coordinator.store.get(eid)
    assert row["status"] == "passed"
    assert row["finalized_at_ns"] is None
    assert coordinator.graph_status(gid)["delivery_status"] == "not_finalized"
    assert coordinator.read(gid)["effective_status"] == "stale"
    with sqlite3.connect(runtime.db) as db:
        audit = db.execute(
            "SELECT previous_finalized_at_ns, reason "
            "FROM final_evaluation_revocations WHERE evaluation_id=?",
            (eid,),
        ).fetchone()
    assert audit is not None
    assert audit[0] > 0 and "repository changed" in audit[1]
