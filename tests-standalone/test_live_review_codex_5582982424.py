"""Regressions from Codex review 5582982424 of no-copy evaluation."""
import asyncio
import time

import pytest

from qiqi_delegate.final_eval_snapshot import _git, inspect_live_roots
from qiqi_delegate.runtime import AgentStartupBlocked
from test_final_evaluation import setup_graph


def test_pre_dispatch_cancel_is_retryable_without_operator_recovery(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("QIQI_EVALUATION_MODE", "live")
    runtime, _, graphs, coordinator, gid = setup_graph(tmp_path)
    scan = coordinator._scan_live
    count = {"n": 0}

    async def cancel_second(roots):
        count["n"] += 1
        if count["n"] == 2:
            raise asyncio.CancelledError()
        return await scan(roots)

    monkeypatch.setattr(coordinator, "_scan_live", cancel_second)
    revision = graphs.get_graph(gid)["revision"]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    row = coordinator.store.latest(gid)
    assert row["status"] == "errored"
    assert row["cleanup"] is None
    assert row["turn_id"] is None
    with runtime._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM write_claims").fetchone()[0] == 0


def test_startup_blocked_recovery_includes_all_claimed_modules(
    tmp_path, monkeypatch,
):
    import qiqi_delegate.runtime as runtime_module

    monkeypatch.setenv("QIQI_EVALUATION_MODE", "live")
    runtime, _, graphs, coordinator, gid = setup_graph(tmp_path)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda _: "/fake/herdr")

    async def ensure():
        return None

    async def create(*_args, **_kwargs):
        return {
            "workspace": {"workspace_id": "blocked-review"},
            "root_pane": {"pane_id": "blocked-pane"},
        }

    async def blocked(*_args, **_kwargs):
        raise AgentStartupBlocked("review-agent", "blocked-pane", "needs trust")

    monkeypatch.setattr(runtime, "_ensure_herdr_server", ensure)
    monkeypatch.setattr(runtime, "_json", create)
    monkeypatch.setattr(runtime, "_start_agent", blocked)
    revision = graphs.get_graph(gid)["revision"]
    with pytest.raises(AgentStartupBlocked) as caught:
        asyncio.run(asyncio.wait_for(
            coordinator.start(gid, "codex-evaluator", revision), timeout=20,
        ))
    commands = caught.value.recovery_command
    assert commands is not None
    lines = commands.splitlines()
    assert len(lines) == 2
    assert "--repository backend " in lines[0]
    assert "--repository frontend " in lines[1]
    assert "--claim-id " in lines[0] and "--claim-id " in lines[1]
    assert lines[0].split("--claim-id ")[1] == lines[1].split("--claim-id ")[1]
    assert all("--worker-termination-confirmed" in line for line in lines)
    assert commands in caught.value.actionable_detail()
    record = coordinator.store.latest(gid)
    assert record["status"] == "interrupted"
    assert record["cleanup"]["write_claim_repositories"] == '["backend", "frontend"]'
    with runtime._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM write_claims").fetchone()[0] == 2


def test_live_git_discovery_rejects_expired_deadline_without_subprocess(
    tmp_path, monkeypatch,
):
    import qiqi_delegate.final_eval_snapshot as snapshot

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Git must not be launched after scan deadline")

    monkeypatch.setattr(snapshot.subprocess, "run", forbidden)
    with pytest.raises(TimeoutError, match="deadline"):
        _git(tmp_path, "ls-files", deadline=time.monotonic() - 1)


def test_live_git_discovery_propagates_deadline_to_all_git_queries(
    tmp_path, monkeypatch,
):
    import qiqi_delegate.final_eval_snapshot as snapshot

    runtime, *_ = setup_graph(tmp_path)
    original = snapshot._git
    calls = []

    def verified(root, *args, deadline=None):
        assert deadline is not None
        assert deadline > time.monotonic()
        calls.append(args)
        return original(root, *args, deadline=deadline)

    monkeypatch.setattr(snapshot, "_git", verified)
    assert set(inspect_live_roots(runtime.repos())) == {"backend", "frontend"}
    assert any(args[0] == "ls-files" for args in calls)
    # HEAD-less Git fixture has no committed tree yet. The rev-parse and
    # index/untracked discovery commands must still share one deadline.
    assert any(args[0] == "rev-parse" for args in calls)
