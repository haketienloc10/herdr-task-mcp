"""Direct multi-module Final Evaluation: no repository clone or JAR copy."""
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from test_final_evaluation import setup_graph, report
from qiqi_delegate.final_eval_snapshot import (
    EvaluationSnapshot, inspect_live_roots, manifest_digest,
)


def _add_tracked_large_jar(runtime):
    root = runtime.repos()["backend"]
    binary = root / "web" / "WEB-INF" / "lib" / "axis-1.3.jar"
    binary.parent.mkdir(parents=True)
    payload = b"jar-data-not-changed" * 75_000  # > legacy 1 MB limit
    binary.write_bytes(payload)
    subprocess.run(
        ["git", "-C", str(root), "add", "-f", "web/WEB-INF/lib/axis-1.3.jar"],
        check=True, timeout=10,
    )
    return binary, payload


def _fake_native(runtime, coordinator, gid, monkeypatch, *, mutate=None):
    import qiqi_delegate.runtime as runtime_module

    monkeypatch.setattr(runtime_module.shutil, "which", lambda _: "/usr/bin/herdr")
    seen = {}

    async def ensure():
        return None

    async def workspace(*args, **kwargs):
        assert args[:3] == ("workspace", "create", "--cwd")
        cwd = Path(args[3])
        seen["cwd"] = cwd
        assert cwd.is_dir()
        source_index = cwd / ".qiqi-final-task-sources" / "index.json"
        graph_archive = cwd / ".qiqi-final-task-sources" / "task-graph.json"
        manifest_file = cwd / ".qiqi-evaluation-manifests.json"
        assert source_index.is_file() and graph_archive.is_file()
        assert manifest_file.is_file()
        contents = json.loads(manifest_file.read_text())["repositories"]
        assert contents["backend"]["inspection_mode"] == "live"
        assert contents["backend"]["root"] == str(runtime.repos()["backend"])
        assert contents["frontend"]["root"] == str(runtime.repos()["frontend"])
        assert len(json.loads(graph_archive.read_text())["task_graph"]) == 2
        assert "Display prices correctly" in json.loads(
            source_index.read_text()
        )["original_user_request"]
        seen["manifest"] = contents
        return {"workspace": {"workspace_id": "live-review-ws"},
                "root_pane": {"pane_id": "live-review-pane"}}

    async def agent_start(*args):
        assert "--add-dir" in args[2]
        for repo in runtime.repos().values():
            assert str(repo) in args[2]
        return "independent-evaluator", {"agent_session": {"kind": "id", "agent": "codex"}}

    async def prompt(*args):
        if mutate:
            mutate()
        return "settled", {"agent_session": {"kind": "id", "agent": "codex"}}

    async def capture(*args):
        current = coordinator.store.latest(gid)
        return {"state": "settled", "agent_response": json.dumps(
            report(current["manifest"])
        )}

    async def close(*args, **kwargs):
        assert args[:2] == ("workspace", "close")
        return (0, "", "")

    monkeypatch.setattr(runtime, "_ensure_herdr_server", ensure)
    monkeypatch.setattr(runtime, "_json", workspace)
    monkeypatch.setattr(runtime, "_start_agent", agent_start)
    monkeypatch.setattr(runtime, "_prompt", prompt)
    monkeypatch.setattr(runtime, "_native_id", lambda *args: "live-native-evaluator")
    monkeypatch.setattr(runtime, "_capture", capture)
    monkeypatch.setattr(runtime, "_run", close)
    return seen


def test_large_unchanged_tracked_jar_does_not_block_multimodule_review(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("QIQI_EVALUATION_MODE", "live")
    runtime, requests, graphs, coordinator, gid = setup_graph(tmp_path)
    jar, payload = _add_tracked_large_jar(runtime)
    before = jar.stat()
    seen = _fake_native(runtime, coordinator, gid, monkeypatch)

    # A clone would call this legacy class. The normal path cannot use it.
    def refuse_snapshot(_self):
        raise AssertionError("repository snapshot must not be created")
    monkeypatch.setattr(EvaluationSnapshot, "__enter__", refuse_snapshot)

    revision = graphs.get_graph(gid)["revision"]
    entry = asyncio.run(asyncio.wait_for(
        coordinator.start(gid, "codex-evaluator", revision), timeout=10,
    ))
    assert entry["status"] == "passed"
    assert entry["cleanup"] is None
    assert entry["manifest"]["backend"]["inspection_mode"] == "live"
    file = next(i for i in entry["manifest"]["backend"]["files"]
                if i["path"] == "web/WEB-INF/lib/axis-1.3.jar")
    assert file["size"] == len(payload)
    assert file["sha256"] == hashlib.sha256(payload).hexdigest()
    assert jar.read_bytes() == payload
    assert jar.stat().st_mtime_ns == before.st_mtime_ns
    assert not seen["cwd"].exists()  # small evaluator context was cleaned
    assert coordinator.finalize(gid, entry["evaluation_id"], revision)["finalized_at_ns"]


def test_live_review_rejects_pass_if_original_source_changes_in_flight(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("QIQI_EVALUATION_MODE", "live")
    runtime, _requests, graphs, coordinator, gid = setup_graph(tmp_path)
    original = runtime.repos()["backend"] / "orders.py"

    def mutate():
        original.write_text(original.read_text() + "\n# changed during review\n")

    _fake_native(runtime, coordinator, gid, monkeypatch, mutate=mutate)
    revision = graphs.get_graph(gid)["revision"]
    entry = asyncio.run(asyncio.wait_for(
        coordinator.start(gid, "codex-evaluator", revision), timeout=10,
    ))
    assert entry["status"] == "inconclusive"
    assert "source changed" in entry["detail"]
    assert entry["report"] is None
    assert not entry["is_current"]
    with pytest.raises(ValueError, match="PASS"):
        coordinator.finalize(gid, entry["evaluation_id"], revision)


def test_live_manifest_hashes_unchanged_binary_and_detects_changes(
    tmp_path, monkeypatch,
):
    runtime, *_ = setup_graph(tmp_path)
    jar, original = _add_tracked_large_jar(runtime)
    first = inspect_live_roots(runtime.repos())
    assert first["backend"]["inspection_mode"] == "live"
    jar.write_bytes(original + b"next-build")
    second = inspect_live_roots(runtime.repos())
    assert manifest_digest(second) != manifest_digest(first)
