"""Codex review #5479599004: final delivery must re-read repos.yaml after commit."""
import sqlite3
import subprocess

import pytest

from qiqi_delegate.final_eval_snapshot import inspect_roots, manifest_digest
from qiqi_delegate.final_eval_store import FinalEvaluationStore
from test_final_evaluation import setup_graph, report


def _persist_pass(runtime, requests, graph_rt, coordinator, graph_run_id):
    graph_revision = graph_rt.get_graph(graph_run_id)["revision"]
    binding = requests.graph_binding(graph_run_id)
    manifest = inspect_roots(runtime.repos())
    entry, created = coordinator.store.reserve(
        graph_run_id, graph_revision,
        binding["request_id"], binding["request_revision"],
        "codex-evaluator", manifest, manifest_digest(manifest),
    )
    assert created
    eid = entry["evaluation_id"]
    coordinator.store.bind_turn(eid, "registry-race-turn")
    payload = report(manifest)
    content = __import__("json").dumps(payload)
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("registry-race-turn", "native-session", "backend",
             "codex-evaluator", "settled", content, 456),
        )
    stored = coordinator.store.complete(
        eid, raw_response=content, report=payload,
        status="passed", turn_id="registry-race-turn",
    )
    assert stored["status"] == "passed"
    return eid, graph_revision


@pytest.mark.parametrize("mutation", ["remap", "remove", "invalid_registry"])
def test_registry_mutation_during_sqlite_commit_revokes_delivery(
    tmp_path, monkeypatch, mutation,
):
    runtime, requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    eid, revision = _persist_pass(runtime, requests, graph_rt, coordinator, gid)
    old_roots = runtime.repos()
    old_digest = manifest_digest(inspect_roots(old_roots))
    config_file = runtime.root / "repos.yaml"
    original = config_file.read_text(encoding="utf-8")
    initial_finalizer = coordinator.store.finalize

    def mutate_registry_after_commit(*args, **kwargs):
        persisted = initial_finalizer(*args, **kwargs)
        assert persisted["finalized_at_ns"]
        if mutation == "remap":
            alternative = tmp_path / "alternate-backend"
            alternative.mkdir()
            subprocess.run(
                ["git", "-C", str(alternative), "init", "-q"], check=True,
            )
            # Same content and Git HEAD, but a DIFFERENT registered Git root.
            # Rechecking only the old cached roots would incorrectly PASS.
            (alternative / "orders.py").write_bytes(
                (old_roots["backend"] / "orders.py").read_bytes()
            )
            config_file.write_text(
                original.replace("../backend", "../alternate-backend"),
                encoding="utf-8",
            )
            assert runtime.repos()["backend"] == alternative.resolve()
        elif mutation == "remove":
            config_file.write_text(
                original.replace(
                    "  - name: backend\n    path: ../backend\n", ""
                ), encoding="utf-8",
            )
            assert "backend" not in runtime.repos()
        else:
            config_file.write_text(
                "repositories:\n  - not-valid: true\n",
                encoding="utf-8",
            )
            with pytest.raises(ValueError):
                runtime.repos()

        # Source files at the original paths did not change. A stale cached
        # root digest still matches, proving the regression's failure mode.
        assert manifest_digest(inspect_roots(old_roots)) == old_digest
        return persisted

    monkeypatch.setattr(coordinator.store, "finalize",
                        mutate_registry_after_commit)
    with pytest.raises(RuntimeError, match="delivery was revoked"):
        coordinator.finalize(gid, eid, revision)

    persisted = FinalEvaluationStore(runtime.db).get(eid)
    assert persisted["status"] == "passed"  # historical verdict retained
    assert persisted["finalized_at_ns"] is None
    assert coordinator.graph_status(gid)["delivery_status"] == "not_finalized"
    assert coordinator.read(gid)["effective_status"] == "stale"
    with sqlite3.connect(runtime.db) as db:
        audit = db.execute(
            "SELECT reason FROM final_evaluation_revocations "
            "WHERE evaluation_id=?", (eid,),
        ).fetchall()
    assert len(audit) == 1
    assert "repository registry" in audit[0][0]


def test_same_registry_roots_after_sqlite_commit_still_finalize(
    tmp_path, monkeypatch,
):
    runtime, requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    eid, revision = _persist_pass(runtime, requests, graph_rt, coordinator, gid)
    file = runtime.root / "repos.yaml"
    original = file.read_text(encoding="utf-8")
    initial_finalizer = coordinator.store.finalize

    def rewrite_registry_without_semantic_change(*args, **kwargs):
        persisted = initial_finalizer(*args, **kwargs)
        file.write_text(original + "\n# comment after finalize\n",
                        encoding="utf-8")
        return persisted

    monkeypatch.setattr(
        coordinator.store, "finalize",
        rewrite_registry_without_semantic_change,
    )
    result = coordinator.finalize(gid, eid, revision)
    assert result["finalized_at_ns"]
    assert coordinator.graph_status(gid)["delivery_status"] == "finalized"
