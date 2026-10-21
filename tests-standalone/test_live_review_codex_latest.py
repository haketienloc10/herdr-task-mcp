"""New Codex review: manifest staging, durable reserve, scanner descendants."""
import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from qiqi_delegate.final_eval_snapshot import scan_live_roots_bounded
from test_final_evaluation import setup_graph
from test_live_final_evaluation import _fake_native


def _scanner_spawns_stuck_git_descendant(roots, _sink):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    Path(roots["pidfile"]).write_text(str(child.pid), encoding="ascii")
    time.sleep(60)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_cancelled_scanner_kills_its_git_descendants(tmp_path):
    pidfile = tmp_path / "git-child.pid"
    with pytest.raises(TimeoutError, match="scanner process timed out"):
        scan_live_roots_bounded(
            {"pidfile": str(pidfile)}, timeout=1.2,
            _worker=_scanner_spawns_stuck_git_descendant,
        )
    assert pidfile.is_file()
    pid = int(pidfile.read_text())
    # SIGKILL may briefly leave a zombie until init reaps it, so a zombie
    # counts as terminated as well as a nonexistent process.
    def still_running():
        if sys.platform.startswith("linux"):
            proc = Path("/proc") / str(pid) / "stat"
            if not proc.exists():
                return False
            return proc.read_text().split()[2] != "Z"
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    for _ in range(30):
        if not still_running():
            break
        time.sleep(0.1)
    assert not still_running(), "scanner timeout left Git descendant running"


def test_live_manifest_staging_does_not_block_mcp_heartbeat(tmp_path, monkeypatch):
    import qiqi_delegate.runtime as runtime_module

    monkeypatch.setenv("QIQI_EVALUATION_MODE", "live")
    runtime, _, graphs, coordinator, gid = setup_graph(tmp_path)
    _fake_native(runtime, coordinator, gid, monkeypatch)
    dumps = runtime_module.json.dumps

    def deliberately_slow_manifest(value, *args, **kwargs):
        if isinstance(value, dict) and "repositories" in value:
            time.sleep(0.7)
        return dumps(value, *args, **kwargs)

    monkeypatch.setattr(runtime_module.json, "dumps", deliberately_slow_manifest)
    revision = graphs.get_graph(gid)["revision"]

    async def exercise():
        ticks = []

        async def heartbeat():
            for _ in range(25):
                await asyncio.sleep(0.04)
                ticks.append(time.monotonic())

        entry, _ = await asyncio.gather(
            coordinator.start(gid, "codex-evaluator", revision),
            heartbeat(),
        )
        assert entry["status"] == "passed"
        assert len(ticks) == 25
        assert max(b-a for a, b in zip(ticks, ticks[1:])) < 0.35

    asyncio.run(asyncio.wait_for(exercise(), timeout=50))


def test_cancel_while_sqlite_reserve_in_progress_drains_and_compensates(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("QIQI_EVALUATION_MODE", "live")
    runtime, _, graph_rt, coord, gid = setup_graph(tmp_path)
    reserved = threading.Event()
    original_reserve = coord.store.reserve

    def slow_reserve(*args, **kwargs):
        reserved.set()
        time.sleep(0.6)
        return original_reserve(*args, **kwargs)

    monkeypatch.setattr(coord.store, "reserve", slow_reserve)

    async def exercise():
        heartbeat = []

        async def ticker():
            for _ in range(18):
                heartbeat.append(time.monotonic())
                await asyncio.sleep(0.05)

        async def runner():
            task = asyncio.create_task(coord.start(
                gid, "codex-evaluator", graph_rt.get_graph(gid)["revision"],
            ))
            while not reserved.is_set():
                await asyncio.sleep(0.03)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        await asyncio.gather(runner(), ticker())
        assert len(heartbeat) == 18
        assert max(b-a for a, b in zip(heartbeat, heartbeat[1:])) < 0.3

    asyncio.run(asyncio.wait_for(exercise(), timeout=40))
    row = coord.store.latest(gid)
    assert row["status"] == "errored"
    assert row["cleanup"] is None
    with runtime._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM write_claims").fetchone()[0] == 0


def test_finalized_status_uses_persisted_live_mode_after_setting_change(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("QIQI_EVALUATION_MODE", "live")
    runtime, _, graphs, coord, gid = setup_graph(tmp_path)
    _fake_native(runtime, coord, gid, monkeypatch)
    revision = graphs.get_graph(gid)["revision"]
    entry = asyncio.run(asyncio.wait_for(
        coord.start(gid, "codex-evaluator", revision), timeout=50,
    ))
    assert entry["status"] == "passed"
    coord.finalize(gid, entry["evaluation_id"], revision)
    monkeypatch.setenv("QIQI_EVALUATION_MODE", "snapshot")
    assert coord.read(gid, entry["evaluation_id"])["is_current"]
    state = coord.graph_status(gid)
    assert state["delivery_status"] == "finalized"
    assert state["evaluation_is_current"] is True
