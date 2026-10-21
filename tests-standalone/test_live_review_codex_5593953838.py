"""Regression coverage for Codex PR review #5593953838."""
import asyncio
import os
import time

import pytest

from qiqi_delegate.final_eval_snapshot import (
    EvaluationSnapshot, manifest_digest, scan_live_roots_bounded,
)
from test_final_evaluation import setup_graph
from test_live_final_evaluation import _fake_native


def _partial_ipc_header_then_stall(_roots, sink):
    # A 4-byte framed message header has arrived, but the pickle body has
    # not. Old reader.poll() -> reader.recv() hangs despite a scan deadline.
    os.write(sink.fileno(), b"\\x00\\x01\\x00\\x00" + b"partial")
    time.sleep(30)


def _oversized_ipc_send_then_stall(_roots, sink):
    # A child trying to send a multi-MiB object can block on the pipe.
    sink.send(("legacy-huge-object", "x" * 4_000_000))
    time.sleep(30)


@pytest.mark.parametrize(
    "worker", [_partial_ipc_header_then_stall, _oversized_ipc_send_then_stall],
)
def test_partial_or_huge_ipc_cannot_bypass_scanner_timeout(worker):
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="scanner process timed out"):
        scan_live_roots_bounded({}, timeout=1.5, _worker=worker)
    assert time.monotonic() - started < 8


def test_live_scan_file_transfer_preserves_manifest_digest(tmp_path):
    runtime, *_ = setup_graph(tmp_path)
    manifest, digest = scan_live_roots_bounded(runtime.repos())
    assert digest == manifest_digest(manifest)
    assert manifest["backend"]["inspection_mode"] == "live"


def test_graph_status_finds_old_live_pass_after_new_snapshot_mode_attempt(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("QIQI_EVALUATION_MODE", "live")
    runtime, _requests, graphs, coordinator, gid = setup_graph(tmp_path)
    _fake_native(runtime, coordinator, gid, monkeypatch)
    revision = graphs.get_graph(gid)["revision"]

    passed = asyncio.run(asyncio.wait_for(
        coordinator.start(gid, "codex-evaluator", revision), timeout=50,
    ))
    assert passed["status"] == "passed"
    coordinator.finalize(gid, passed["evaluation_id"], revision)

    # Create a newer attempt in snapshot mode at source state B.
    original_file = runtime.repos()["backend"] / "orders.py"
    state_a = original_file.read_bytes()
    original_file.write_bytes(state_a + b"\\n# different source B\\n")
    graph = graphs.get_graph(gid)
    request = coordinator.task_requests.get(
        coordinator.task_requests.graph_binding(gid)["request_id"]
    )
    with EvaluationSnapshot(runtime.repos()) as snap:
        newer, created = coordinator.store.reserve(
            gid, graph["revision"], request["request_id"],
            request["revision"], "codex-evaluator",
            snap.manifest, snap.digest,
        )
        assert created
        coordinator.store.complete(
            newer["evaluation_id"], raw_response=None, report=None,
            status="errored", detail="synthetic snapshot attempt at B",
        )
    assert coordinator.store.latest(gid)["evaluation_id"] == newer["evaluation_id"]

    # Revert to original A. The earlier finalized live PASS is valid again
    # even though latest attempt uses snapshot inspection semantics.
    original_file.write_bytes(state_a)
    assert coordinator.read(gid, passed["evaluation_id"])["is_current"]
    summary = coordinator.graph_status(gid)
    assert summary["final_evaluation_id"] == passed["evaluation_id"]
    assert summary["delivery_status"] == "finalized"
    assert summary["evaluation_is_current"] is True
    assert summary["final_evaluation_status"] == "passed"
