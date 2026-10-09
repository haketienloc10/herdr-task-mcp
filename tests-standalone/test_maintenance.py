"""Safe operator-only recovery of stale write claims."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from qiqi_delegate.maintenance import release_stale_claim, show_claim
from qiqi_delegate.runtime import DelegateRuntime


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
