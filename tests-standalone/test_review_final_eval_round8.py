"""Codex review #5479874398: Git deletion evidence and stable finalized delivery."""
import asyncio
import json
import sqlite3
import subprocess

import pytest

from qiqi_delegate.final_eval_snapshot import (
    EvaluationSnapshot, evidence_is_in_manifest, inspect_roots, manifest_digest,
)
from test_final_evaluation import evidence, report, setup_graph


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True,
        text=True, check=True,
    ).stdout


def _committed_deletion(runtime, *, staged=False):
    root = runtime.repos()["backend"]
    path = root / "obsolete_api.py"
    path.write_text("def obsolete(): return True\n", encoding="utf-8")
    _git(root, "add", "obsolete_api.py")
    _git(root, "-c", "user.name=Evaluator Tests",
         "-c", "user.email=eval@example.invalid", "commit",
         "-m", "Track API to remove")
    path.unlink()
    if staged:
        _git(root, "add", "-u", "--", "obsolete_api.py")
    return path


@pytest.mark.parametrize("staged", [False, True])
def test_deletion_only_requirement_can_pass_with_verified_tombstone(tmp_path, staged):
    runtime, _requests, _graph, coordinator, _gid = setup_graph(tmp_path)
    deleted_file = _committed_deletion(runtime, staged=staged)
    with EvaluationSnapshot(runtime.repos()) as snapshot:
        manifest = snapshot.manifest
        assert "obsolete_api.py" in manifest["backend"]["deleted_paths"]
        assert not (snapshot.paths["backend"] / "obsolete_api.py").exists()
        index = json.loads(
            (snapshot.paths["backend"] /
             ".qiqi-evaluation-manifest.json").read_text(encoding="utf-8")
        )
        assert "obsolete_api.py" in index["deleted_paths"]
        tombstone = {
            "kind": "deleted", "repository": "backend",
            "path": "obsolete_api.py", "locator": "git:tracked-deletion",
        }
        # No fictional file hash: the path must be an actual HEAD/index
        # tracked deletion missing from the frozen working tree.
        assert evidence_is_in_manifest(
            manifest, "backend", "obsolete_api.py", kind="deleted"
        )
        payload = report(manifest)
        payload["requirement_results"][0]["evidence"] = [tombstone]
        checked = coordinator.store.validate_report(payload, manifest, {"R1"})
        assert checked["verdict"] == "pass"
        # Cross-repo checks may cite deletion evidence alongside remaining files.
        payload["cross_repository_checks"][0]["evidence"] = [
            tombstone, evidence(manifest, "frontend"),
        ]
        assert coordinator.store.validate_report(payload, manifest, {"R1"})[
            "verdict"
        ] == "pass"
    deleted_file.write_text("def obsolete(): return True\n", encoding="utf-8")
    assert "obsolete_api.py" not in inspect_roots(
        runtime.repos()
    )["backend"]["deleted_paths"]


def test_unknown_or_forged_tombstone_never_authorizes_pass(tmp_path):
    runtime, _req, _graph, coordinator, _gid = setup_graph(tmp_path)
    _committed_deletion(runtime)
    manifest = inspect_roots(runtime.repos())
    base = report(manifest)
    valid = {
        "kind": "deleted", "repository": "backend",
        "path": "obsolete_api.py", "locator": "git:tracked-deletion",
    }
    cases = [
        {**valid, "path": "not_tracked.py"},
        {**valid, "path": "orders.py"},  # file exists; not deleted
        {**valid, "repository": "frontend"},
        {**valid, "sha256": "0" * 64},  # deleted files have no hash
        {**valid, "kind": "file"},  # kind/file mismatch
        {**valid, "kind": "deleted", "locator": ""},
        {**valid, "path": None},
    ]
    for invalid in cases:
        bad = report(manifest)
        bad["requirement_results"][0]["evidence"] = [invalid]
        with pytest.raises(ValueError, match="evidence"):
            coordinator.store.validate_report(bad, manifest, {"R1"})
    base["requirement_results"][0]["evidence"] = [valid]
    assert coordinator.store.validate_report(base, manifest, {"R1"})[
        "verdict"
    ] == "pass"


def test_native_deletion_only_pass_can_finalize_and_becomes_stale_if_restored(
    tmp_path, monkeypatch,
):
    runtime, _requests, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    deleted_file = _committed_deletion(runtime)
    calls = []

    async def fake_native(**kwargs):
        calls.append(kwargs["evaluation_id"])
        coordinator.store.bind_turn(kwargs["evaluation_id"], "deleted-api-native")
        manifest = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        body = report(manifest)
        body["requirement_results"][0]["evidence"] = [{
            "kind": "deleted", "repository": "backend",
            "path": "obsolete_api.py", "locator": "tracked file removed",
        }]
        captured = json.dumps(body)
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("deleted-api-native", "native-evaluator", "backend",
                 "codex-evaluator", "settled", captured, 123),
            )
        return {"state": "settled", "turn_id": "deleted-api-native",
                "agent_response": captured}

    monkeypatch.setattr(runtime, "delegate", fake_native)
    rev = graph_runtime.get_graph(gid)["revision"]
    approved = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    assert approved["status"] == "passed"
    assert coordinator.finalize(gid, approved["evaluation_id"], rev)[
        "finalized_at_ns"
    ]
    assert len(calls) == 1
    deleted_file.write_text("def obsolete(): return True\n", encoding="utf-8")
    assert coordinator.graph_status(gid)["delivery_status"] == "not_finalized"
    assert coordinator.read(gid)["effective_status"] == "stale"


def test_repeat_start_on_same_finalized_snapshot_does_not_redispatch_or_hide_delivery(
    tmp_path, monkeypatch,
):
    runtime, _reqs, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    calls = []

    async def fake_native(**kwargs):
        calls.append(kwargs["evaluation_id"])
        coordinator.store.bind_turn(kwargs["evaluation_id"], "finalized-native")
        current = coordinator.store.get(kwargs["evaluation_id"])
        response = json.dumps(report(current["manifest"]))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("finalized-native", "native-evaluator", "backend",
                 "codex-evaluator", "settled", response, 123),
            )
        return {"state": "settled", "turn_id": "finalized-native",
                "agent_response": response}

    monkeypatch.setattr(runtime, "delegate", fake_native)
    rev = graph_runtime.get_graph(gid)["revision"]
    first = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    coordinator.finalize(gid, first["evaluation_id"], rev)
    assert coordinator.graph_status(gid)["delivery_status"] == "finalized"
    assert coordinator.store.latest(gid)["evaluation_id"] == first["evaluation_id"]
    for _ in range(3):
        reused = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
        assert reused["evaluation_id"] == first["evaluation_id"]
        assert reused["already_finalized"] is True
        assert reused["is_current"] is True
        assert reused["finalized_at_ns"]
        assert reused["status"] == "passed"
        assert coordinator.graph_status(gid)["delivery_status"] == "finalized"
    assert calls == [first["evaluation_id"]]
    assert coordinator.store.latest(gid)["evaluation_id"] == first["evaluation_id"]
    with sqlite3.connect(runtime.db) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM final_evaluations WHERE graph_run_id=?", (gid,),
        ).fetchone()[0] == 1


def test_older_current_finalization_keeps_delivery_if_a_newer_attempt_exists(
    tmp_path, monkeypatch,
):
    runtime, requests, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    calls = []

    async def fake_native(**kwargs):
        calls.append(kwargs["evaluation_id"])
        coordinator.store.bind_turn(kwargs["evaluation_id"], "first-finished")
        current = coordinator.store.get(kwargs["evaluation_id"])
        response = json.dumps(report(current["manifest"]))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("first-finished", "native-evaluator", "backend",
                 "codex-evaluator", "settled", response, 123),
            )
        return {"state": "settled", "turn_id": "first-finished",
                "agent_response": response}

    monkeypatch.setattr(runtime, "delegate", fake_native)
    rev = graph_runtime.get_graph(gid)["revision"]
    first = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    binding = requests.graph_binding(gid)
    manifest = inspect_roots(runtime.repos())
    newer, created = coordinator.store.reserve(
        gid, rev, binding["request_id"], binding["request_revision"],
        "codex-evaluator", manifest, manifest_digest(manifest),
    )
    assert created and newer["evaluation_id"] != first["evaluation_id"]
    coordinator.finalize(gid, first["evaluation_id"], rev)
    assert coordinator.store.latest(gid)["status"] == "requested"
    # Latest "requested" cannot erase a finalized exact snapshot from get_graph.
    assert coordinator.graph_status(gid)["delivery_status"] == "finalized"
    reused = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    assert reused["already_finalized"]
    assert reused["evaluation_id"] == first["evaluation_id"]
    assert len(calls) == 1


def test_changed_worktree_does_not_reuse_stale_finalization(tmp_path, monkeypatch):
    runtime, _reqs, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    calls = []

    async def fake_native(**kwargs):
        eid = kwargs["evaluation_id"]
        calls.append(eid)
        tid = "turn-" + str(len(calls))
        coordinator.store.bind_turn(eid, tid)
        body = json.dumps(report(coordinator.store.get(eid)["manifest"]))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tid, "new-session", "backend", "codex-evaluator",
                 "settled", body, 987),
            )
        return {"state": "settled", "turn_id": tid,
                "agent_response": body}

    monkeypatch.setattr(runtime, "delegate", fake_native)
    rev = graph_runtime.get_graph(gid)["revision"]
    approved = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    coordinator.finalize(gid, approved["evaluation_id"], rev)
    assert coordinator.graph_status(gid)["delivery_status"] == "finalized"
    target = runtime.repos()["backend"] / "orders.py"
    target.write_text(target.read_text() + "\n# changed since PASS\n")
    assert coordinator.graph_status(gid)["delivery_status"] == "not_finalized"
    newer = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    assert newer["status"] == "passed"
    assert newer["evaluation_id"] != approved["evaluation_id"]
    assert len(calls) == 2
    assert coordinator.graph_status(gid)["delivery_status"] == "not_finalized"
    coordinator.finalize(gid, newer["evaluation_id"], rev)
    assert coordinator.graph_status(gid)["delivery_status"] == "finalized"
