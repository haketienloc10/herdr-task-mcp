"""Regression tests for native Final Evaluation's lightweight Git freshness."""
import asyncio
import json
import sqlite3
import subprocess

import pytest

from qiqi_delegate.final_eval_git_state import review_scope
from qiqi_delegate.final_eval_manifest import manifest_digest
from test_final_evaluation import report, setup_graph


def _git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
    )


@pytest.mark.parametrize("index_flag", ["--skip-worktree", "--assume-unchanged"])
def test_flagged_tracked_edits_invalidate_final_pass(tmp_path, monkeypatch, index_flag):
    """A Git diff may hide edits but Final PASS must not remain current."""
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    path = backend / "orders.py"
    _git(backend, "add", "--", "orders.py")
    _git(backend, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "Track evaluator source")
    _git(backend, "update-index", index_flag, "--", "orders.py")

    async def delegate(**kwargs):
        coordinator.store.bind_turn(kwargs["evaluation_id"], "flagged-native")
        scope = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        body = json.dumps(report(scope))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("flagged-native", "native-evaluator", "backend",
                 "codex-evaluator", "settled", body, 153),
            )
        return {"state": "settled", "turn_id": "flagged-native",
                "agent_response": body}

    monkeypatch.setattr(runtime, "delegate", delegate)
    revision = graph_rt.get_graph(gid)["revision"]
    evaluated = asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    assert evaluated["status"] == "passed"
    before = manifest_digest(review_scope(runtime.repos()))

    path.write_text("def total(n):\n    return n * 999\n", encoding="utf-8")
    after = manifest_digest(review_scope(runtime.repos()))
    assert before != after, "Git index flags must not hide tracked working-tree edits"
    assert coordinator.read(gid, evaluated["evaluation_id"])["effective_status"] == "stale"
    with pytest.raises((RuntimeError, ValueError), match="PASS"):
        coordinator.finalize(gid, evaluated["evaluation_id"], revision)


def test_untracked_content_cannot_cross_file_record_boundaries(tmp_path):
    """Formerly equal byte streams must yield different Git fingerprints."""
    runtime, _requests, _graph_rt, _coordinator, _gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    a = backend / "a"
    b = backend / "b"
    a.write_bytes(b"")
    b.write_bytes(b"")
    # Old encoding was: path length + path + file mode + raw file bytes.
    b_header = (1).to_bytes(8, "big") + b"b" + b.stat().st_mode.to_bytes(8, "big")
    a.write_bytes(b"")
    b.write_bytes(b_header)
    first = manifest_digest(review_scope(runtime.repos()))
    a.write_bytes(b_header)
    b.write_bytes(b"")
    second = manifest_digest(review_scope(runtime.repos()))
    assert first != second
