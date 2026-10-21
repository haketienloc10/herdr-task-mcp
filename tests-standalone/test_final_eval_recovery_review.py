"""Regression coverage for crash windows and partially failed native cleanup."""
import asyncio
from pathlib import Path

import pytest

from test_final_evaluation import setup_graph
from qiqi_delegate.maintenance import recover_final_evaluation


def _mock_native(monkeypatch, runtime, *, workspace_create, start=None):
    import qiqi_delegate.runtime as runtime_module

    monkeypatch.setattr(runtime_module.shutil, "which", lambda _: "/usr/bin/herdr")

    async def ensure_server():
        return None

    async def native_start(*_args):
        raise RuntimeError("native launch failed")

    async def close(*args, **_kw):
        assert args[:2] == ("workspace", "close")
        return (0, "", "")

    monkeypatch.setattr(runtime, "_ensure_herdr_server", ensure_server)
    monkeypatch.setattr(runtime, "_json", workspace_create)
    monkeypatch.setattr(runtime, "_start_agent", start or native_start)
    monkeypatch.setattr(runtime, "_run", close)


def test_lost_herdr_create_response_preserves_discoverable_label(
    tmp_path, monkeypatch,
):
    runtime, _, graph_rt, coord, gid = setup_graph(tmp_path)
    trust = tmp_path / "trusted-create"
    monkeypatch.setenv("QIQI_EVALUATOR_CWD", str(trust))
    seen = {}

    async def uncertain_create(*argv, **_kwargs):
        assert argv[:3] == ("workspace", "create", "--cwd")
        seen["cwd"] = Path(argv[3])
        seen["label"] = argv[argv.index("--label") + 1]
        assert seen["label"].startswith("qiqi:evaluator:")
        # Before issuing the external create, the exact lookup label and
        # cwd must already be available to recovery from SQLite.
        row = coord.store.latest(gid)
        assert row["cleanup"]["workspace_label"] == seen["label"]
        assert row["cleanup"]["evaluator_cwd"] == str(seen["cwd"])
        raise RuntimeError("Herdr accepted create but response was lost")

    _mock_native(monkeypatch, runtime, workspace_create=uncertain_create)
    with pytest.raises(RuntimeError, match="outcome unknown"):
        asyncio.run(coord.start(
            gid, "claude-evaluator", graph_rt.get_graph(gid)["revision"]
        ))
    row = coord.store.latest(gid)
    assert row["status"] == "interrupted"
    cleanup = row["cleanup"]
    assert cleanup["workspace_label"] == seen["label"]
    assert cleanup["workspace_id"] == ""
    assert cleanup["evaluator_cwd"] == str(seen["cwd"])
    assert Path(cleanup["capture_sink"]).is_dir()
    assert "snapshot_root" not in cleanup
    assert (trust / "runs" / ".qiqi-active").is_file()

    # In production, operator inspects 'herdr workspace list', matches the
    # exact label and cwd, closes the workspace if present, and verifies exit.
    row = recover_final_evaluation(
        workspace=runtime.root, evaluation_id=row["evaluation_id"],
        worker_termination_confirmed=True,
    )
    assert row["status"] == "errored"
    assert not seen["cwd"].exists()
    assert not (trust / "runs" / ".qiqi-active").exists()


def test_local_cleanup_failure_keeps_context_sink_and_lease(
    tmp_path, monkeypatch,
):
    import qiqi_delegate.runtime as runtime_module

    runtime, _, graph_rt, coord, gid = setup_graph(tmp_path)
    trust = tmp_path / "trusted-local-cleanup"
    monkeypatch.setenv("QIQI_EVALUATOR_CWD", str(trust))
    observed = {}

    async def create(*argv, **_kwargs):
        observed["cwd"] = Path(argv[3])
        return {
            "workspace": {"workspace_id": "closed-workspace-77"},
            "root_pane": {"pane_id": "p77"},
        }

    _mock_native(monkeypatch, runtime, workspace_create=create)
    original_cleanup = runtime_module.cleanup_temporary_tree

    def fail_only_sink(path):
        if Path(path).name.startswith("qiqi-result-"):
            raise PermissionError("simulated Windows file-lock error")
        return original_cleanup(path)

    monkeypatch.setattr(runtime_module, "cleanup_temporary_tree", fail_only_sink)
    with pytest.raises(RuntimeError, match="local cleanup failed"):
        asyncio.run(coord.start(
            gid, "claude-evaluator", graph_rt.get_graph(gid)["revision"]
        ))
    row = coord.store.latest(gid)
    assert row["status"] == "interrupted"
    cleanup = row["cleanup"]
    assert cleanup["cleanup_state"] == "local_cleanup_failed"
    assert cleanup["workspace_id"] == "closed-workspace-77"
    assert cleanup["workspace_label"].startswith("qiqi:evaluator:")
    assert cleanup["evaluator_cwd"] == str(observed["cwd"])
    assert Path(cleanup["capture_sink"]).is_dir()
    assert "snapshot_root" not in cleanup
    assert observed["cwd"].is_dir()
    assert (trust / "runs" / ".qiqi-active").exists()

    # Maintenance uses the unpatched original deletion function.
    recovered = recover_final_evaluation(
        workspace=runtime.root, evaluation_id=row["evaluation_id"],
        worker_termination_confirmed=True,
    )
    assert recovered["status"] == "errored"
    assert not Path(cleanup["capture_sink"]).exists()
    assert "snapshot_root" not in cleanup
    assert not observed["cwd"].exists()
    assert not (trust / "runs" / ".qiqi-active").exists()
