"""Codex #5594779060: post-hash identity and durable delivery races."""
import asyncio
import subprocess

import pytest

from test_final_evaluation import setup_graph
from test_live_final_evaluation import _fake_native


def _change_identity(change, runtime, requests, gid, tmp_path):
    if change == "request_revision":
        binding = requests.graph_binding(gid)
        with runtime._connect() as db:
            db.execute(
                "UPDATE task_requests SET revision=revision+1 WHERE request_id=?",
                (binding["request_id"],),
            )
    elif change == "registered_root":
        alternate = tmp_path / "backend_remapped"
        alternate.mkdir()
        subprocess.run(
            ["git", "-C", str(alternate), "init", "-q"],
            check=True, timeout=10,
        )
        (runtime.root / "repos.yaml").write_text(
            "repositories:\n"
            "  - name: backend\n    path: ../backend_remapped\n"
            "  - name: frontend\n    path: ../frontend\n",
            encoding="utf-8",
        )
    else:
        raise AssertionError(change)


def _passed(tmp_path, monkeypatch):
    monkeypatch.setenv("QIQI_EVALUATION_MODE", "live")
    runtime, requests, graphs, coordinator, gid = setup_graph(tmp_path)
    _fake_native(runtime, coordinator, gid, monkeypatch)
    rev = graphs.get_graph(gid)["revision"]
    evaluation = asyncio.run(asyncio.wait_for(
        coordinator.start(gid, "codex-evaluator", rev), timeout=50,
    ))
    assert evaluation["status"] == "passed"
    return runtime, requests, graphs, coordinator, gid, rev, evaluation


@pytest.mark.parametrize("change", ["request_revision", "registered_root"])
def test_read_rechecks_full_identity_after_matching_digest(
    tmp_path, monkeypatch, change,
):
    runtime, requests, _, coord, gid, _, evaluation = _passed(tmp_path, monkeypatch)
    original = coord._digest_for_row
    triggered = {"done": False}

    def matching_then_change(roots, row):
        digest = original(roots, row)
        if not triggered["done"]:
            triggered["done"] = True
            _change_identity(change, runtime, requests, gid, tmp_path)
        return digest

    monkeypatch.setattr(coord, "_digest_for_row", matching_then_change)
    row = coord.read(gid, evaluation["evaluation_id"])
    assert triggered["done"]
    assert row["is_current"] is False
    assert row["effective_status"] == "stale"


def test_graph_status_rechecks_durable_finalized_flag_after_digest(
    tmp_path, monkeypatch,
):
    runtime, _, _, coord, gid, rev, evaluation = _passed(tmp_path, monkeypatch)
    coord.finalize(gid, evaluation["evaluation_id"], rev)
    original = coord._digest_for_row
    revoked = {"done": False}

    def matching_then_revoke(roots, row):
        digest = original(roots, row)
        if not revoked["done"]:
            revoked["done"] = True
            coord.store.revoke_finalization(
                evaluation["evaluation_id"],
                reason="concurrent finalizer revoked delivery",
            )
        return digest

    monkeypatch.setattr(coord, "_digest_for_row", matching_then_revoke)
    result = coord.graph_status(gid)
    assert revoked["done"]
    assert coord.store.get(evaluation["evaluation_id"])["finalized_at_ns"] is None
    assert result["delivery_status"] == "not_finalized"
    assert result["evaluation_is_current"] is False
    assert result["final_evaluation_status"] == "stale"


@pytest.mark.parametrize("change", ["request_revision", "registered_root"])
def test_finalize_rechecks_identity_after_its_last_digest(
    tmp_path, monkeypatch, change,
):
    runtime, requests, _, coord, gid, rev, evaluation = _passed(tmp_path, monkeypatch)
    original_digest = coord._digest_for_row
    original_finalize = coord.store.finalize
    state = {"committed": False, "changed": False}

    def finalize_and_mark(*args, **kwargs):
        result = original_finalize(*args, **kwargs)
        state["committed"] = True
        return result

    def digest_then_change(roots, row):
        digest = original_digest(roots, row)
        if state["committed"] and not state["changed"]:
            state["changed"] = True
            _change_identity(change, runtime, requests, gid, tmp_path)
        return digest

    monkeypatch.setattr(coord.store, "finalize", finalize_and_mark)
    monkeypatch.setattr(coord, "_digest_for_row", digest_then_change)
    with pytest.raises(RuntimeError, match="delivery was revoked"):
        coord.finalize(gid, evaluation["evaluation_id"], rev)

    assert state["committed"] and state["changed"]
    assert coord.store.get(evaluation["evaluation_id"])["finalized_at_ns"] is None
