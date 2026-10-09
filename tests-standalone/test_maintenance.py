"""Safe operator-only recovery of stale write claims."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from qiqi_delegate.maintenance import (
    recover_interrupted_attempt, release_stale_claim, show_attempt, show_claim,
)
from qiqi_delegate.runtime import DelegateRuntime
from qiqi_delegate.task_graph_runtime import GraphRuntime, task_graph_from_payload
from qiqi_delegate.task_graph_store import GraphRuntimeStore


def setup_workspace(tmp_path):
    for name in ("backend", "frontend"):
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(
            ["git", "-C", str(repo), "init", "-q"], check=True,
            capture_output=True, text=True,
        )
    (tmp_path / "repos.yaml").write_text(
        "repositories:\n"
        "  - name: backend\n"
        "    path: backend\n"
        "  - name: frontend\n"
        "    path: frontend\n"
    )
    return DelegateRuntime(tmp_path)


def test_operator_recovery_requires_termination_and_exact_pair(tmp_path):
    runtime = setup_workspace(tmp_path)
    runtime._claim("backend", "turn:blocked")
    runtime._claim("frontend", "turn:other")

    assert show_claim(workspace=tmp_path, repository="backend")["claim_id"] == "turn:blocked"
    with pytest.raises(ValueError, match="worker has stopped"):
        release_stale_claim(
            workspace=tmp_path, repository="backend",
            claim_id="turn:blocked", worker_termination_confirmed=False,
        )
    with pytest.raises(RuntimeError, match="claim_id mismatch"):
        release_stale_claim(
            workspace=tmp_path, repository="backend",
            claim_id="turn:different", worker_termination_confirmed=True,
        )
    with pytest.raises(ValueError, match="unregistered repository"):
        release_stale_claim(
            workspace=tmp_path, repository="unknown",
            claim_id="turn:blocked", worker_termination_confirmed=True,
        )

    with runtime._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM write_claims").fetchone()[0] == 2

    result = release_stale_claim(
        workspace=tmp_path, repository="backend",
        claim_id="turn:blocked", worker_termination_confirmed=True,
    )
    assert result == {
        "repository": "backend", "claim_id": "turn:blocked", "released": True,
    }
    assert show_claim(workspace=tmp_path, repository="backend")["claim_id"] is None
    assert show_claim(workspace=tmp_path, repository="frontend")["claim_id"] == "turn:other"
    with runtime._connect() as db:
        rows = db.execute(
            "SELECT repository, claim_id, worker_termination_confirmed "
            "FROM write_claim_recovery_audit"
        ).fetchall()
        assert len(rows) == 1
        assert tuple(rows[0]) == ("backend", "turn:blocked", 1)

    with pytest.raises(RuntimeError, match="no write claim"):
        release_stale_claim(
            workspace=tmp_path, repository="backend",
            claim_id="turn:blocked", worker_termination_confirmed=True,
        )
    runtime._claim("backend", "turn:new-worker")
    with pytest.raises(RuntimeError, match="claim_id mismatch"):
        release_stale_claim(
            workspace=tmp_path, repository="backend",
            claim_id="turn:blocked", worker_termination_confirmed=True,
        )
    assert show_claim(workspace=tmp_path, repository="backend")["claim_id"] == "turn:new-worker"


def test_maintenance_cli_guard_and_exact_release(tmp_path):
    runtime = setup_workspace(tmp_path)
    runtime._claim("backend", "turn:blocked")
    base = [
        sys.executable, "-m", "qiqi_delegate.maintenance",
        "release-claim", "--workspace", str(tmp_path),
        "--repository", "backend", "--claim-id", "turn:blocked",
    ]
    denied = subprocess.run(base, capture_output=True, text=True)
    assert denied.returncode == 1
    assert "worker has stopped" in denied.stderr
    with runtime._connect() as db:
        assert db.execute(
            "SELECT claim_id FROM write_claims WHERE repository='backend'"
        ).fetchone()[0] == "turn:blocked"

    shown = subprocess.run(
        [
            sys.executable, "-m", "qiqi_delegate.maintenance", "show-claim",
            "--workspace", str(tmp_path), "--repository", "backend",
        ],
        capture_output=True, text=True,
    )
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["claim_id"] == "turn:blocked"

    released = subprocess.run(
        [*base, "--worker-termination-confirmed"],
        capture_output=True, text=True,
    )
    assert released.returncode == 0, released.stderr
    assert json.loads(released.stdout)["released"] is True

    runtime._claim("backend", "turn:next")
    with pytest.raises(RuntimeError, match="busy"):
        runtime._claim("backend", "turn:conflict")
    assert show_claim(workspace=tmp_path, repository="backend")["claim_id"] == "turn:next"


def test_exact_operator_attempt_recovery_is_audited_and_unblocks_wave(tmp_path):
    runtime = setup_workspace(tmp_path)
    store = GraphRuntimeStore(runtime.db)
    graph = GraphRuntime(store)
    started = graph.start_graph(task_graph_from_payload({"nodes": [{
        "node_id": "B1", "repository": "backend", "route": "codex-balanced",
        "task_packet": {
            "objective": "Work", "scope": ["src"],
            "acceptance_criteria": ["Tests pass"],
        },
    }]}), repository_names={"backend", "frontend"})
    run = started["graph_run_id"]
    (attempt,) = store.start_wave(
        run, "wave-1", expected_revision=started["revision"],
        attempts=({"node_id": "B1"},),
    )
    # A pre-dispatch process crash leaves the wave fail-closed.
    assert graph.get_graph(run)["graph_state"] == "running"
    shown = show_attempt(
        workspace=tmp_path, repository="backend", graph_run_id=run,
        attempt_id=attempt,
    )
    assert shown["dispatch_state"] == "prepared"
    assert shown["wave_id"] == "wave-1"
    args = dict(
        workspace=tmp_path, repository="backend", graph_run_id=run,
        wave_id="wave-1", node_id="B1", attempt_id=attempt,
        worker_termination_confirmed=True,
    )
    with pytest.raises(ValueError, match="termination"):
        recover_interrupted_attempt(**{**args, "worker_termination_confirmed": False})
    with pytest.raises(RuntimeError, match="exact active wave"):
        recover_interrupted_attempt(**{**args, "wave_id": "another"})
    with pytest.raises(RuntimeError, match="exact running wave attempt"):
        recover_interrupted_attempt(**{**args, "attempt_id": "unknown"})
    with pytest.raises(RuntimeError, match="authored graph"):
        recover_interrupted_attempt(**{**args, "repository": "frontend"})

    runtime._claim("backend", "turn:possibly-alive")
    with pytest.raises(RuntimeError, match="still has a write claim"):
        recover_interrupted_attempt(**args)
    assert store.get_attempt(attempt)["runtime_state"] == "running"
    release_stale_claim(
        workspace=tmp_path, repository="backend",
        claim_id="turn:possibly-alive", worker_termination_confirmed=True,
    )
    done = recover_interrupted_attempt(**args)
    assert done["recovered"] is True and done["wave_closed"] is True
    assert graph.get_graph(run)["graph_state"] == "awaiting_review"
    assert graph.get_graph(run)["current_wave_id"] is None
    assert store.get_attempt(attempt)["result"]["failure_type"] == (
        "operator_recovered_interrupted_attempt"
    )
    with runtime._connect() as db:
        row = db.execute(
            "SELECT graph_run_id, wave_id, node_id, attempt_id, repository, "
            "worker_termination_confirmed FROM graph_attempt_recovery_audit"
        ).fetchone()
    assert tuple(row) == (run, "wave-1", "B1", attempt, "backend", 1)
    with pytest.raises(RuntimeError, match="exact active wave"):
        recover_interrupted_attempt(**args)

    # Both the inspection and terminalization commands work in the installed venv.
    inspection = subprocess.run([
        sys.executable, "-m", "qiqi_delegate.maintenance", "show-attempt",
        "--workspace", str(tmp_path), "--repository", "backend",
        "--graph-run-id", run, "--attempt-id", attempt,
    ], capture_output=True, text=True)
    assert inspection.returncode == 0, inspection.stderr
    assert json.loads(inspection.stdout)["runtime_state"] == "failed"


def test_recover_one_attempt_keeps_wave_open_until_all_siblings_finish(tmp_path):
    runtime = setup_workspace(tmp_path)
    store = GraphRuntimeStore(runtime.db)
    graph = GraphRuntime(store)
    started = graph.start_graph(task_graph_from_payload({"nodes": [{
        "node_id": name, "repository": name, "route": "codex-balanced",
        "task_packet": {"objective": "Work", "scope": ["src"],
                        "acceptance_criteria": ["Tests pass"]},
    } for name in ("backend", "frontend")]}),
        repository_names={"backend", "frontend"},
    )
    run = started["graph_run_id"]
    ids = store.start_wave(
        run, "wave-two", expected_revision=started["revision"],
        attempts=({"node_id": "backend"}, {"node_id": "frontend"}),
    )
    for index, repo in enumerate(("backend", "frontend")):
        cli = [
            sys.executable, "-m", "qiqi_delegate.maintenance", "recover-attempt",
            "--workspace", str(tmp_path), "--repository", repo,
            "--graph-run-id", run, "--wave-id", "wave-two",
            "--node-id", repo, "--attempt-id", ids[index],
        ]
        denied = subprocess.run(cli, capture_output=True, text=True)
        assert denied.returncode != 0
        assert store.get_attempt(ids[index])["runtime_state"] == "running"
        recovered = subprocess.run(
            [*cli, "--worker-termination-confirmed"],
            capture_output=True, text=True,
        )
        assert recovered.returncode == 0, recovered.stderr
        assert json.loads(recovered.stdout)["wave_closed"] is (index == 1)
    assert graph.get_graph(run)["graph_state"] == "awaiting_review"
    assert store.get_run(run)["current_wave_id"] is None
    with runtime._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM graph_attempt_recovery_audit"
        ).fetchone()[0] == 2

def test_renaming_registry_entry_cannot_bypass_canonical_claim(tmp_path):
    runtime = setup_workspace(tmp_path)
    runtime._claim("backend", "turn:old-worker")
    (tmp_path / "repos.yaml").write_text(
        "repositories:\n"
        "  - name: renamed\n    path: backend\n"
        "  - name: frontend\n    path: frontend\n"
    )
    renamed = DelegateRuntime(tmp_path)
    with pytest.raises(RuntimeError, match="busy"):
        renamed._claim("renamed", "turn:new-worker")
    with renamed._connect() as db:
        rows = db.execute(
            "SELECT repository, repository_root FROM write_claims"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["repository"] == "backend"
        assert rows[0]["repository_root"] == str((tmp_path / "backend").resolve())

    # The currently registered alias can inspect/release the old holder only
    # by matching the same canonical root and exact original claim ID.
    assert show_claim(
        workspace=tmp_path, repository="renamed"
    )["claim_id"] == "turn:old-worker"
    with pytest.raises(RuntimeError, match="claim_id mismatch"):
        release_stale_claim(
            workspace=tmp_path, repository="renamed",
            claim_id="turn:not-old", worker_termination_confirmed=True,
        )
    release_stale_claim(
        workspace=tmp_path, repository="renamed",
        claim_id="turn:old-worker", worker_termination_confirmed=True,
    )
    renamed._claim("renamed", "turn:new-worker")
    with renamed._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM write_claims").fetchone()[0] == 1
        assert db.execute(
            "SELECT repository FROM write_claims"
        ).fetchone()[0] == "renamed"


def test_unmapped_legacy_claim_blocks_writes_until_exact_manual_recovery(tmp_path):
    runtime = setup_workspace(tmp_path)
    # Model an active claim from the previous version, whose repository root
    # was never recorded. A name may have changed since this row was written.
    with runtime._connect() as db:
        db.execute("DROP TABLE write_claims")
        db.execute(
            "CREATE TABLE write_claims (repository TEXT PRIMARY KEY, "
            "claim_id TEXT NOT NULL, created_at_ns INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT INTO write_claims VALUES ('backend', 'turn:legacy', 1)"
        )
    (tmp_path / "repos.yaml").write_text(
        "repositories:\n"
        "  - name: renamed\n    path: backend\n"
        "  - name: frontend\n    path: frontend\n"
    )
    migrated = DelegateRuntime(tmp_path)
    with migrated._connect() as db:
        row = db.execute(
            "SELECT repository_root FROM write_claims "
            "WHERE repository = 'backend'"
        ).fetchone()
        assert row[0] is None

    with pytest.raises(RuntimeError, match="unmapped legacy write claim"):
        migrated._claim("renamed", "turn:new")
    # Even unrelated repositories are held during ambiguous legacy state.
    with pytest.raises(RuntimeError, match="unmapped legacy write claim"):
        migrated._claim("frontend", "turn:other")
    assert show_claim(
        workspace=tmp_path, repository="backend"
    )["claim_id"] == "turn:legacy"
    release_stale_claim(
        workspace=tmp_path, repository="backend",
        claim_id="turn:legacy", worker_termination_confirmed=True,
    )
    migrated._claim("renamed", "turn:new")
    with migrated._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM write_claims").fetchone()[0] == 1

