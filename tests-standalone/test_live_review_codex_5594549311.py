"""Codex #5594549311: cross-mode freshness, RAM manifest, task archives."""
import asyncio
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from qiqi_delegate.final_eval_snapshot import (
    EvaluationSnapshot, scan_live_roots_bounded, stage_task_archive_bounded,
)
from test_final_evaluation import setup_graph
from test_live_final_evaluation import _fake_native


def _unresponsive_workspace_reader(*_args):
    time.sleep(30)


def test_archive_staging_worker_can_be_killed_while_file_io_blocks():
    active = {p.pid for p in multiprocessing.active_children()}
    with pytest.raises(TimeoutError, match="archive staging timed out"):
        stage_task_archive_bounded(_unresponsive_workspace_reader, (), timeout=0.4)
    assert {p.pid for p in multiprocessing.active_children()} == active


def test_live_scanner_avoids_tmpdir_file_reads(tmp_path, monkeypatch):
    import qiqi_delegate.final_eval_snapshot as snapshot

    runtime, *_ = setup_graph(tmp_path)

    def forbidden_temp_directory(*_args, **_kwargs):
        raise AssertionError("scanner must not read manifest from TMPDIR")

    monkeypatch.setattr(snapshot.tempfile, "TemporaryDirectory", forbidden_temp_directory)
    manifest, digest = scan_live_roots_bounded(runtime.repos())
    assert set(manifest) == {"backend", "frontend"}
    assert len(digest) == 64


def test_live_archive_staging_keeps_mcp_loop_responsive(
    tmp_path, monkeypatch,
):
    import qiqi_delegate.final_eval as evaluation

    monkeypatch.setenv("QIQI_EVALUATION_MODE", "live")
    runtime, _, graphs, coordinator, gid = setup_graph(tmp_path)
    _fake_native(runtime, coordinator, gid, monkeypatch)
    real_stager = evaluation.stage_task_archive_bounded

    def slow_archive(*args, **kwargs):
        time.sleep(0.65)
        return real_stager(*args, **kwargs)

    monkeypatch.setattr(evaluation, "stage_task_archive_bounded", slow_archive)
    revision = graphs.get_graph(gid)["revision"]

    async def run():
        stamps = []

        async def heartbeat():
            for _ in range(24):
                stamps.append(time.monotonic())
                await asyncio.sleep(0.04)

        entry, _ = await asyncio.gather(
            coordinator.start(gid, "codex-evaluator", revision),
            heartbeat(),
        )
        assert entry["status"] == "passed"
        assert len(stamps) == 24
        assert max(b-a for a, b in zip(stamps, stamps[1:])) < 0.3

    asyncio.run(asyncio.wait_for(run(), timeout=50))


def test_historical_pass_is_rechecked_after_intervening_mode_scan(
    tmp_path, monkeypatch,
):
    import qiqi_delegate.final_eval as evaluation

    monkeypatch.setenv("QIQI_EVALUATION_MODE", "live")
    runtime, requests, graphs, coordinator, gid = setup_graph(tmp_path)
    source = runtime.repos()["backend"] / "orders.py"
    state_a = source.read_bytes()
    graph = graphs.get_graph(gid)
    binding = requests.graph_binding(gid)
    task = requests.get(binding["request_id"])
    revision = graph["revision"]

    # Historical snapshot PASS of state A, committed in SQLite.
    with EvaluationSnapshot(runtime.repos()) as snapshot:
        old, created = coordinator.store.reserve(
            gid, revision, task["request_id"], task["revision"],
            "codex-evaluator", snapshot.manifest, snapshot.digest,
        )
        assert created
        # This regression tests status *selection*, not native capture
        # authorization. Seed a historical finalized row directly because
        # production's PASS transition requires a real persisted Herdr turn.
        coordinator.store.complete(
            old["evaluation_id"], raw_response=None, report=None,
            status="errored", detail="synthetic historical delivery",
        )
        with coordinator.store._connect() as db:
            db.execute(
                "UPDATE final_evaluations SET status='passed', "
                "finalized_at_ns=? WHERE evaluation_id=?",
                (time.time_ns(), old["evaluation_id"]),
            )

    # Historical live PASS of state B.
    source.write_bytes(state_a + b"\n# state B\n")
    _fake_native(runtime, coordinator, gid, monkeypatch)
    live = asyncio.run(asyncio.wait_for(
        coordinator.start(gid, "codex-evaluator", revision), timeout=50,
    ))
    assert live["status"] == "passed"
    coordinator.finalize(gid, live["evaluation_id"], revision)

    # Latest attempt is snapshot-mode error at state C.
    source.write_bytes(state_a + b"\n# state C\n")
    with EvaluationSnapshot(runtime.repos()) as snapshot:
        latest, created = coordinator.store.reserve(
            gid, revision, task["request_id"], task["revision"],
            "codex-evaluator", snapshot.manifest, snapshot.digest,
        )
        assert created
        coordinator.store.complete(
            latest["evaluation_id"], raw_response=None,
            report=None, status="errored", detail="synthetic C",
        )

    # Begin status query at state A. Snapshot mode is cached as A, but
    # the live-mode scanner changes source to C before the older snapshot
    # A PASS is encountered. Recheck must reject cached A.
    source.write_bytes(state_a)
    real_scan = evaluation.scan_live_roots_bounded
    mutated = {"once": False}

    def live_scan_and_mutate(roots):
        result = real_scan(roots)
        if not mutated["once"]:
            mutated["once"] = True
            source.write_bytes(state_a + b"\n# state C\n")
        return result

    monkeypatch.setattr(evaluation, "scan_live_roots_bounded", live_scan_and_mutate)
    status = coordinator.graph_status(gid)
    assert mutated["once"]
    assert status["delivery_status"] == "not_finalized"
    assert not status["evaluation_is_current"]
