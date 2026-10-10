"""Review #5479788911: evaluator claims remain recoverable after snapshot cleanup."""
import asyncio
import sqlite3

import pytest

from qiqi_delegate.core import build_task_packet
from qiqi_delegate.final_eval_snapshot import EvaluationSnapshot
from qiqi_delegate.maintenance import release_stale_claim, show_claim
from qiqi_delegate.runtime import AgentStartupBlocked
from test_final_evaluation import setup_graph


def _reserve(runtime, requests, graph_rt, coordinator, gid, snap):
    graph = graph_rt.get_graph(gid)
    binding = requests.graph_binding(gid)
    item, created = coordinator.store.reserve(
        gid, graph["revision"], binding["request_id"],
        binding["request_revision"], "codex-evaluator",
        snap.manifest, snap.digest,
    )
    assert created
    return item["evaluation_id"]


def _packet():
    return build_task_packet(
        objective="Independently evaluate final deliverable",
        scope=["backend", "frontend"],
        acceptance_criteria=["Inspect source in both modules"],
    )


def test_preserved_evaluator_claim_recoverable_after_snapshot_disappears(
    tmp_path, monkeypatch,
):
    import qiqi_delegate.runtime as runtime_module

    runtime, requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    originals = runtime.repos()
    monkeypatch.setattr(runtime_module.shutil, "which", lambda _: "/fake/herdr")

    async def ensure():
        return None

    async def workspace(*args, **_kw):
        assert args[:3] == ("workspace", "create", "--cwd")
        assert args[3] != str(originals["backend"])
        return {"workspace": {"workspace_id": "preserved-evaluator"},
                "root_pane": {"pane_id": "blocked-pane"}}

    async def blocked(_pane, _adapter, _argv):
        raise AgentStartupBlocked("blocked-codex", "blocked-pane", "approval pending")

    monkeypatch.setattr(runtime, "_ensure_herdr_server", ensure)
    monkeypatch.setattr(runtime, "_json", workspace)
    monkeypatch.setattr(runtime, "_start_agent", blocked)

    with EvaluationSnapshot(originals) as snap:
        copy_root = snap.paths["backend"]
        assert copy_root != originals["backend"]
        eid = _reserve(runtime, requests, graph_rt, coordinator, gid, snap)
        with pytest.raises(AgentStartupBlocked) as captured:
            asyncio.run(runtime.delegate(
                repository="backend", route="codex-evaluator",
                packet=_packet(),
                evaluation_repositories=("backend", "frontend"),
                evaluation_roots=snap.paths, evaluation_id=eid,
            ))
        assert "release-claim" in str(captured.value)
        assert str(copy_root) not in captured.value.recovery_command
        assert copy_root.is_dir()
        with sqlite3.connect(runtime.db) as db:
            row = db.execute(
                "SELECT claim_id, repository_root FROM write_claims "
                "WHERE repository='backend'",
            ).fetchone()
        assert row is not None
        claim_id, registered_root = row
        assert claim_id.startswith("turn:")
        assert registered_root == str(originals["backend"])
        assert coordinator.store.get(eid)["turn_id"] == claim_id.removeprefix("turn:")

    assert not copy_root.exists()
    inspected = show_claim(workspace=runtime.root, repository="backend")
    assert inspected["claim_id"] == claim_id
    with pytest.raises(RuntimeError, match="busy"):
        runtime._claim("backend", "turn:new-peer", repository_root=originals["backend"])
    with pytest.raises(ValueError, match="worker has stopped"):
        release_stale_claim(
            workspace=runtime.root, repository="backend", claim_id=claim_id,
            worker_termination_confirmed=False,
        )
    with pytest.raises(RuntimeError, match="mismatch"):
        release_stale_claim(
            workspace=runtime.root, repository="backend", claim_id="turn:wrong",
            worker_termination_confirmed=True,
        )
    released = release_stale_claim(
        workspace=runtime.root, repository="backend", claim_id=claim_id,
        worker_termination_confirmed=True,
    )
    assert released["released"] is True
    assert show_claim(workspace=runtime.root, repository="backend")["claim_id"] is None
    with sqlite3.connect(runtime.db) as db:
        audit = db.execute(
            "SELECT claim_id, worker_termination_confirmed "
            "FROM write_claim_recovery_audit WHERE repository='backend'",
        ).fetchone()
    assert audit == (claim_id, 1)
    coordinator.store.complete(
        eid, raw_response=None, report=None, status="interrupted",
        detail="blocked external native agent",
    )
    coordinator.store.release_interrupted(
        eid, worker_termination_confirmed=True,
    )
    runtime._claim("backend", "turn:new-peer", repository_root=originals["backend"])
    assert runtime.release_claim("backend", "turn:new-peer")


@pytest.mark.parametrize("close_fails", [False, True])
def test_close_cleanup_or_recovery_uses_registered_root(
    tmp_path, monkeypatch, close_fails,
):
    import qiqi_delegate.runtime as runtime_module

    runtime, requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    originals = runtime.repos()
    monkeypatch.setattr(runtime_module.shutil, "which", lambda _: "/fake/herdr")

    async def ensure():
        return None

    async def workspace(*_args, **_kwargs):
        return {"workspace": {"workspace_id": "evaluator-workspace"},
                "root_pane": {"pane_id": "evaluator-pane"}}

    async def failed_launch(*_args, **_kwargs):
        raise RuntimeError("deliberately failed native agent launch")

    async def close(*args, **_kw):
        assert args[:2] == ("workspace", "close")
        if close_fails:
            raise RuntimeError("Herdr close unconfirmed")
        return (0, "", "")

    monkeypatch.setattr(runtime, "_ensure_herdr_server", ensure)
    monkeypatch.setattr(runtime, "_json", workspace)
    monkeypatch.setattr(runtime, "_start_agent", failed_launch)
    monkeypatch.setattr(runtime, "_run", close)
    with EvaluationSnapshot(originals) as snap:
        copy_root = snap.paths["backend"]
        eid = _reserve(runtime, requests, graph_rt, coordinator, gid, snap)
        with pytest.raises(RuntimeError, match="deliberately failed|close unconfirmed"):
            asyncio.run(runtime.delegate(
                repository="backend", route="codex-evaluator",
                packet=_packet(), evaluation_id=eid,
                evaluation_repositories=("backend", "frontend"),
                evaluation_roots=snap.paths,
            ))
        with sqlite3.connect(runtime.db) as db:
            row = db.execute(
                "SELECT claim_id, repository_root FROM write_claims "
                "WHERE repository='backend'",
            ).fetchone()
        if close_fails:
            assert row is not None
            assert row[1] == str(originals["backend"])
        else:
            assert row is None

    assert not copy_root.exists()
    if close_fails:
        claim_id = row[0]
        assert show_claim(workspace=runtime.root, repository="backend")["claim_id"] == claim_id
        assert release_stale_claim(
            workspace=runtime.root, repository="backend", claim_id=claim_id,
            worker_termination_confirmed=True,
        )["released"]
    assert show_claim(workspace=runtime.root, repository="backend")["claim_id"] is None
