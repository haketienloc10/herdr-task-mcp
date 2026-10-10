"""Codex review #5479962043: index-only absences are not Git deletions."""
import asyncio
import subprocess

import pytest

from qiqi_delegate.final_eval_snapshot import (
    EvaluationSnapshot, evidence_is_in_manifest, inspect_roots,
)
from test_final_evaluation import setup_graph


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def commit_baseline(root):
    git(root, "add", "--", "orders.py")
    git(
        root, "-c", "user.name=Evaluator Tests",
        "-c", "user.email=eval@example.invalid",
        "commit", "-m", "Committed baseline",
    )


@pytest.mark.parametrize("has_head", [False, True])
@pytest.mark.parametrize("index_state", ["intent_to_add", "staged_new"])
def test_absent_index_only_path_never_becomes_deletion_evidence(
    tmp_path, has_head, index_state,
):
    runtime, _requests, _graph, _coordinator, _gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    if has_head:
        commit_baseline(backend)
    else:
        with pytest.raises(subprocess.CalledProcessError):
            git(backend, "rev-parse", "--verify", "HEAD")

    file = backend / "phantom.py"
    file.write_text("NEW_UNCOMMITTED_CODE = True\n", encoding="utf-8")
    if index_state == "intent_to_add":
        git(backend, "add", "-N", "--", "phantom.py")
    else:
        git(backend, "add", "--", "phantom.py")
    file.unlink()

    # An index record for a new file does NOT imply there was ever a real
    # previous file in HEAD, even when the file is temporarily absent.
    assert "phantom.py" in git(backend, "ls-files", "--cached")
    if has_head:
        assert "phantom.py" not in git(
            backend, "ls-tree", "-r", "--name-only", "HEAD",
        )

    with pytest.raises(ValueError, match="missing index-only path"):
        inspect_roots({"backend": backend})
    with pytest.raises(ValueError, match="missing index-only path"):
        with EvaluationSnapshot({"backend": backend}):
            pass

    # Once the new source exists again, its CURRENT bytes are valid evidence
    # and must never be misrepresented as a deletion tombstone.
    file.write_text("RESTORED_NEW_FILE = True\n", encoding="utf-8")
    with EvaluationSnapshot({"backend": backend}) as snapshot:
        row = snapshot.manifest["backend"]
        assert "phantom.py" not in row["deleted_paths"]
        assert "phantom.py" in {x["path"] for x in row["files"]}
        assert not evidence_is_in_manifest(
            snapshot.manifest, "backend", "phantom.py", kind="deleted"
        )


@pytest.mark.parametrize("staged", [False, True])
def test_genuine_head_deletion_still_produces_valid_tombstone(tmp_path, staged):
    runtime, _req, _graph, coordinator, _gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    doomed = backend / "legacy_feature.py"
    doomed.write_text("OLD_FEATURE = True\n", encoding="utf-8")
    git(backend, "add", "--", "legacy_feature.py")
    git(
        backend, "-c", "user.name=Evaluator Tests",
        "-c", "user.email=eval@example.invalid",
        "commit", "-m", "Track feature before removal",
    )
    doomed.unlink()
    if staged:
        git(backend, "add", "-u", "--", "legacy_feature.py")
        assert "legacy_feature.py" not in git(
            backend, "ls-files", "--cached"
        )

    with EvaluationSnapshot({"backend": backend}) as snapshot:
        assert "legacy_feature.py" in snapshot.manifest["backend"]["deleted_paths"]
        assert evidence_is_in_manifest(
            snapshot.manifest, "backend", "legacy_feature.py", kind="deleted"
        )
    # Keep existing multi-repo report and PASS verification unchanged.
    manifest = inspect_roots(runtime.repos())
    from test_final_evaluation import report
    payload = report(manifest)
    payload["requirement_results"][0]["evidence"] = [{
        "kind": "deleted", "repository": "backend",
        "path": "legacy_feature.py", "locator": "HEAD tracked deletion",
    }]
    assert coordinator.store.validate_report(
        payload, manifest, {"R1"}
    )["verdict"] == "pass"


def test_index_only_absence_fails_before_evaluation_reservation_or_native_dispatch(
    tmp_path, monkeypatch,
):
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    commit_baseline(backend)
    phantom = backend / "phantom.py"
    phantom.write_text("PHANTOM = True\n", encoding="utf-8")
    git(backend, "add", "-N", "--", "phantom.py")
    phantom.unlink()

    attempted = []

    async def should_never_launch(**kwargs):
        attempted.append(kwargs)
        raise AssertionError("Evaluator cannot accept index-only tombstone")

    monkeypatch.setattr(runtime, "delegate", should_never_launch)
    revision = graph_rt.get_graph(gid)["revision"]
    with pytest.raises(ValueError, match="missing index-only path"):
        asyncio.run(
            coordinator.start(gid, "codex-evaluator", revision)
        )
    assert attempted == []
    assert coordinator.store.latest(gid) is None
