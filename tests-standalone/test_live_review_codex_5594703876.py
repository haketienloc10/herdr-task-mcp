"""Codex review 5594703876: eligibility changes during the final status hash."""
import asyncio
import subprocess

import pytest

from test_final_evaluation import setup_graph
from test_live_final_evaluation import _fake_native


@pytest.mark.parametrize("change", ["request_revision", "registered_root"])
def test_status_rechecks_request_and_registry_after_final_digest(
    tmp_path, monkeypatch, change,
):
    monkeypatch.setenv("QIQI_EVALUATION_MODE", "live")
    runtime, requests, graphs, coordinator, gid = setup_graph(tmp_path)
    _fake_native(runtime, coordinator, gid, monkeypatch)
    revision = graphs.get_graph(gid)["revision"]
    evaluation = asyncio.run(asyncio.wait_for(
        coordinator.start(gid, "codex-evaluator", revision), timeout=50,
    ))
    assert evaluation["status"] == "passed"
    coordinator.finalize(gid, evaluation["evaluation_id"], revision)

    original_digest = coordinator._digest_for_row
    updated = {"done": False}

    def digest_then_change_identity(roots, row):
        digest = original_digest(roots, row)
        if not updated["done"]:
            updated["done"] = True
            if change == "request_revision":
                binding = requests.graph_binding(gid)
                with runtime._connect() as db:
                    db.execute(
                        "UPDATE task_requests SET revision=revision+1 "
                        "WHERE request_id=?",
                        (binding["request_id"],),
                    )
            else:
                # New registered backend with same name, but a different Git
                # root. Hash of OLD root still matches before remapping.
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
        return digest

    monkeypatch.setattr(coordinator, "_digest_for_row", digest_then_change_identity)
    status = coordinator.graph_status(gid)

    assert updated["done"], "final verification scan must run"
    assert status["final_evaluation_id"] == evaluation["evaluation_id"]
    assert status["final_evaluation_status"] == "stale"
    assert status["delivery_status"] == "not_finalized"
    assert status["evaluation_is_current"] is False
