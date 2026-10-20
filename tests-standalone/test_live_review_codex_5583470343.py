"""Codex #5583470343: killable scans, off-loop digest, mode parity."""
import asyncio
import multiprocessing
import time

import pytest

from qiqi_delegate.final_eval_snapshot import (
    manifest_digest, scan_live_roots_bounded,
)
from test_final_evaluation import setup_graph
from test_live_final_evaluation import _fake_native


def _blocked_read_stand_in(_roots, _sink):
    """Reproduce an unresponsive worker (the real trigger may be os.read)."""
    time.sleep(30)


def test_scanner_process_is_terminated_after_blocked_read_timeout():
    before = {item.pid for item in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="scanner process timed out"):
        scan_live_roots_bounded({}, timeout=0.4, _worker=_blocked_read_stand_in)
    assert time.monotonic() - started < 5
    assert {item.pid for item in multiprocessing.active_children()} == before


def test_live_scan_returns_digest_from_child_and_leaves_loop_responsive(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("QIQI_EVALUATION_MODE", "live")
    runtime, _, _, coordinator, _ = setup_graph(tmp_path)

    async def run():
        ticks = []

        async def heartbeat():
            for _ in range(30):
                await asyncio.sleep(0.03)
                ticks.append(time.monotonic())

        (manifest, digest), _ = await asyncio.gather(
            coordinator._scan_live(runtime.repos()), heartbeat(),
        )
        assert set(manifest) == {"backend", "frontend"}
        assert digest == manifest_digest(manifest)
        assert len(ticks) == 30
        return digest

    assert asyncio.run(asyncio.wait_for(run(), timeout=30))


def test_graph_status_normalizes_live_mode_whitespace(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("QIQI_EVALUATION_MODE", " live ")
    runtime, _, graphs, coordinator, gid = setup_graph(tmp_path)
    _fake_native(runtime, coordinator, gid, monkeypatch)
    revision = graphs.get_graph(gid)["revision"]

    async def execute():
        return await coordinator.start(gid, "codex-evaluator", revision)

    entry = asyncio.run(asyncio.wait_for(execute(), timeout=45))
    assert entry["status"] == "passed"
    finalized = coordinator.finalize(gid, entry["evaluation_id"], revision)
    assert finalized["finalized_at_ns"]
    current = coordinator.graph_status(gid)
    assert current["delivery_status"] == "finalized"
    assert current["evaluation_is_current"] is True
    assert current["final_evaluation_status"] == "passed"
