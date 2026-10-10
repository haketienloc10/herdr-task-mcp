"""Review #5479925855: sparse checkout omissions are not deletion tombstones."""
import asyncio
import subprocess

import pytest

from qiqi_delegate.final_eval_snapshot import (
    EvaluationSnapshot, inspect_roots,
)
from test_final_evaluation import setup_graph


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=True,
    ).stdout


def commit_two_paths(root):
    (root / "src").mkdir(exist_ok=True)
    (root / "excluded").mkdir(exist_ok=True)
    (root / "src" / "keep.py").write_text("ENABLED = True\n", encoding="utf-8")
    (root / "excluded" / "missing.py").write_text(
        "STILL_IN_GIT = True\n", encoding="utf-8"
    )
    git(root, "add", "--", "src/keep.py", "excluded/missing.py")
    git(
        root, "-c", "user.name=Evaluation Tests",
        "-c", "user.email=eval@example.invalid",
        "commit", "-m", "Track files before sparse checkout",
    )


def test_manual_skip_worktree_absence_is_not_a_deletion(tmp_path):
    runtime, _requests, _graph, _coordinator, _gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    commit_two_paths(backend)

    missing = backend / "excluded" / "missing.py"
    git(backend, "update-index", "--skip-worktree", "--", "excluded/missing.py")
    missing.unlink()

    assert not missing.exists()
    assert "S excluded/missing.py" in git(backend, "ls-files", "-t")

    with pytest.raises(ValueError, match="skip-worktree"):
        inspect_roots({"backend": backend})
    with pytest.raises(ValueError, match="skip-worktree"):
        with EvaluationSnapshot({"backend": backend}):
            pass

    # Once the skip-worktree marker is removed, the same tracked absence
    # means an actual unstaged deletion and must remain eligible evidence.
    git(backend, "update-index", "--no-skip-worktree", "--",
        "excluded/missing.py")
    with EvaluationSnapshot({"backend": backend}) as snapshot:
        assert "excluded/missing.py" in snapshot.manifest["backend"]["deleted_paths"]
        assert not (snapshot.paths["backend"] / "excluded/missing.py").exists()


@pytest.mark.parametrize("sparse_index", [False, True])
def test_real_sparse_checkout_cannot_issue_tombstones_for_excluded_paths(
    tmp_path, sparse_index,
):
    runtime, _requests, _graph, _coordinator, _gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    commit_two_paths(backend)

    git(backend, "sparse-checkout", "init", "--cone")
    if sparse_index:
        git(backend, "sparse-checkout", "set", "--sparse-index", "src")
    else:
        git(backend, "sparse-checkout", "set", "src")

    assert (backend / "src" / "keep.py").exists()
    assert not (backend / "excluded" / "missing.py").exists()
    assert git(backend, "config", "--bool", "--get",
               "core.sparseCheckout").strip() == "true"
    with pytest.raises(ValueError, match="sparse checkout"):
        inspect_roots({"backend": backend})
    with pytest.raises(ValueError, match="sparse checkout"):
        with EvaluationSnapshot({"backend": backend}):
            pass


def test_sparse_checkout_fails_before_evaluator_reservation_or_dispatch(tmp_path, monkeypatch):
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    commit_two_paths(backend)
    git(backend, "sparse-checkout", "init", "--cone")
    git(backend, "sparse-checkout", "set", "src")
    called = []

    async def should_not_dispatch(**kwargs):
        called.append(kwargs)
        raise AssertionError("sparse checkout cannot be reviewed as a deletion")

    monkeypatch.setattr(runtime, "delegate", should_not_dispatch)
    graph_revision = graph_rt.get_graph(gid)["revision"]
    with pytest.raises(ValueError, match="sparse checkout"):
        asyncio.run(
            coordinator.start(gid, "codex-evaluator", graph_revision)
        )
    assert called == []
    assert coordinator.store.latest(gid) is None


def test_core_sparse_checkout_mode_fails_even_when_all_files_are_present(tmp_path):
    runtime, _requests, _graph, _coordinator, _gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    commit_two_paths(backend)
    git(backend, "config", "core.sparseCheckout", "true")
    # Prevent a sparse-index operation from silently treating a staged
    # deletion in HEAD as a product removal, even if the index contains
    # no skipped paths by the time final evaluation runs.
    assert (backend / "excluded" / "missing.py").exists()
    assert "S " not in git(backend, "ls-files", "-t")
    with pytest.raises(ValueError, match="sparse checkout"):
        inspect_roots({"backend": backend})
