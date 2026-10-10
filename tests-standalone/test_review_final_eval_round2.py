"""Regressions for Codex review #5479424204: real cross-repo checks and deletions."""
import asyncio
import json
import sqlite3
import subprocess

import pytest

from qiqi_delegate.final_eval_snapshot import (
    EvaluationSnapshot, inspect_roots, manifest_digest,
)
from test_final_evaluation import setup_graph, evidence, report


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def commit(root, relative):
    git(root, "add", "--", relative)
    git(root, "-c", "user.name=Evaluator Test",
        "-c", "user.email=eval@example.invalid",
        "commit", "-m", "Track fixture before evaluation")


def test_split_single_repo_checks_cannot_fake_a_cross_repo_pass(tmp_path):
    runtime, _requests, _graph, coordinator, _id = setup_graph(tmp_path)
    manifest = inspect_roots(runtime.repos())
    split = report(manifest)
    split["cross_repository_checks"] = [
        {
            "name": "Backend only",
            "status": "pass",
            "evidence": [evidence(manifest, "backend")],
            "rationale": "Inspected backend alone",
        },
        {
            "name": "Frontend only",
            "status": "pass",
            "evidence": [evidence(manifest, "frontend")],
            "rationale": "Inspected frontend alone",
        },
    ]
    # The union still covers BOTH registered repositories. This cannot stand
    # in for one genuine integration check that inspected both sides.
    assert {
        item["repository"]
        for check in split["cross_repository_checks"]
        for item in check["evidence"]
    } == set(manifest)
    with pytest.raises(ValueError, match="claimed PASS"):
        coordinator.store.validate_report(split, manifest, {"R1"})

    split["cross_repository_checks"] = [{
        "name": "Fake integration with duplicate backend evidence",
        "status": "pass",
        "evidence": [evidence(manifest, "backend"), evidence(manifest, "backend")],
        "rationale": "Same repository twice",
    }]
    with pytest.raises(ValueError, match="claimed PASS"):
        coordinator.store.validate_report(split, manifest, {"R1"})

    # One actual cross-module check with evidence from both repos is allowed.
    checked = coordinator.store.validate_report(
        report(manifest), manifest, {"R1"}
    )
    assert checked["verdict"] == "pass"


def test_multiple_real_integration_checks_must_cover_every_graph_repo(tmp_path):
    runtime, _requests, _graph, coordinator, _id = setup_graph(tmp_path)
    manifest = inspect_roots(runtime.repos())
    # Third repo participates in the evaluated graph in this synthetic report.
    # Distinct evidence paths and SHA-256 identities remain validated.
    manifest["database"] = {
        **manifest["backend"],
        "root": str(runtime.root.parent / "database"),
    }
    payload = report(manifest)
    with pytest.raises(ValueError, match="claimed PASS"):
        coordinator.store.validate_report(payload, manifest, {"R1"})
    payload["cross_repository_checks"].append({
        "name": "Frontend and database contract",
        "status": "pass",
        "evidence": [evidence(manifest, "frontend"),
                     evidence(manifest, "database")],
        "rationale": "Compared frontend and database",
    })
    assert coordinator.store.validate_report(
        payload, manifest, {"R1"}
    )["verdict"] == "pass"


def test_unstaged_tracked_deletion_is_recorded_and_does_not_abort_snapshot(tmp_path):
    runtime, _requests, _graph, _coordinator, _id = setup_graph(tmp_path)
    roots = runtime.repos()
    backend = roots["backend"]
    doomed = backend / "legacy_contract.py"
    doomed.write_text("OLD_API = True\n", encoding="utf-8")
    commit(backend, "legacy_contract.py")

    original = inspect_roots(roots)
    assert "legacy_contract.py" not in original["backend"]["deleted_paths"]
    doomed.unlink()

    after = inspect_roots(roots)
    assert "legacy_contract.py" in after["backend"]["deleted_paths"]
    assert "legacy_contract.py" not in {
        item["path"] for item in after["backend"]["files"]
    }
    assert manifest_digest(after) != manifest_digest(original)

    with EvaluationSnapshot(roots) as snapshot:
        assert snapshot.manifest == after
        assert not (snapshot.paths["backend"] / "legacy_contract.py").exists()
        metadata = json.loads(
            (snapshot.paths["backend"] /
             ".qiqi-evaluation-manifest.json").read_text(encoding="utf-8")
        )
        assert "legacy_contract.py" in metadata["deleted_paths"]

    # Restoring a deleted tracked file MUST invalidate the previous digest.
    doomed.write_text("OLD_API = True\n", encoding="utf-8")
    assert manifest_digest(inspect_roots(roots)) == manifest_digest(original)


def test_staged_deletion_is_recorded_even_after_it_disappears_from_index(tmp_path):
    runtime, _requests, _graph, _coordinator, _id = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    doomed = backend / "removed_endpoint.py"
    doomed.write_text("ENDPOINT = '/old'\n", encoding="utf-8")
    commit(backend, "removed_endpoint.py")
    doomed.unlink()
    git(backend, "add", "-u", "--", "removed_endpoint.py")
    assert git(backend, "ls-files", "--cached", "--", "removed_endpoint.py") == ""
    with EvaluationSnapshot({"backend": backend}) as snapshot:
        assert "removed_endpoint.py" in (
            snapshot.manifest["backend"]["deleted_paths"]
        )
        assert not (snapshot.paths["backend"] / "removed_endpoint.py").exists()


@pytest.mark.parametrize("stage_rename", [False, True])
def test_unstaged_and_staged_renames_capture_new_file_and_old_tombstone(
    tmp_path, stage_rename,
):
    runtime, _requests, _graph, _coordinator, _id = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    old = backend / "old_api.py"
    new = backend / "new_api.py"
    old.write_text("def api(): return 'new'\n", encoding="utf-8")
    commit(backend, "old_api.py")
    old.rename(new)
    if stage_rename:
        git(backend, "add", "-A")
    with EvaluationSnapshot({"backend": backend}) as snapshot:
        manifest = snapshot.manifest["backend"]
        assert "old_api.py" in manifest["deleted_paths"]
        assert "new_api.py" in {item["path"] for item in manifest["files"]}
        assert not (snapshot.paths["backend"] / "old_api.py").exists()
        assert (snapshot.paths["backend"] / "new_api.py").read_text(
            encoding="utf-8"
        ) == "def api(): return 'new'\n"


def test_restoring_deleted_file_makes_existing_pass_stale(tmp_path, monkeypatch):
    runtime, _requests, graph_runtime, coordinator, graph_run_id = setup_graph(
        tmp_path
    )
    backend = runtime.repos()["backend"]
    old = backend / "removed_feature.py"
    old.write_text("def removed(): return 1\n", encoding="utf-8")
    commit(backend, "removed_feature.py")
    old.unlink()

    async def fake_delegate(**kwargs):
        evaluation_id = kwargs["evaluation_id"]
        coordinator.store.bind_turn(evaluation_id, "deleted-native-turn")
        manifest = coordinator.store.get(evaluation_id)["manifest"]
        assert "removed_feature.py" in manifest["backend"]["deleted_paths"]
        body = json.dumps(report(manifest))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("deleted-native-turn", "native-evaluator", "backend",
                 "codex-evaluator", "settled", body, 42),
            )
        return {"state": "settled", "turn_id": "deleted-native-turn",
                "agent_response": body}

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    rev = graph_runtime.get_graph(graph_run_id)["revision"]
    evaluated = asyncio.run(
        coordinator.start(graph_run_id, "codex-evaluator", rev)
    )
    assert evaluated["status"] == "passed"
    assert evaluated["is_current"] is True
    old.write_text("def removed(): return 1\n", encoding="utf-8")
    assert coordinator.read(graph_run_id)["effective_status"] == "stale"
    with pytest.raises(ValueError, match="PASS"):
        coordinator.finalize(graph_run_id, evaluated["evaluation_id"], rev)
