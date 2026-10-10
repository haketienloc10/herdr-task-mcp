"""Codex #5479830092: a captured PASS is not valid after unconfirmed Herdr close."""
import asyncio
import json
import sqlite3

import pytest

from qiqi_delegate.final_eval_store import FinalEvaluationStore
from qiqi_delegate.maintenance import (
    show_claim, release_stale_claim, recover_final_evaluation,
)
from test_final_evaluation import setup_graph, report


@pytest.mark.parametrize("close_unconfirmed", [False, True])
def test_native_settled_capture_with_unconfirmed_close_cannot_pass(
    tmp_path, monkeypatch, close_unconfirmed,
):
    """Exercise REAL DelegateRuntime.delegate cleanup return, not a fake response."""
    import qiqi_delegate.runtime as runtime_module

    runtime, requests, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    originals = runtime.repos()
    monkeypatch.setattr(runtime_module.shutil, "which", lambda _: "/fake/herdr")
    observed = {}

    async def fake_server():
        return None

    async def workspace(*args, **_kwargs):
        observed["cwd"] = args[3]
        assert args[:3] == ("workspace", "create", "--cwd")
        return {"workspace": {"workspace_id": "preserved-native-workspace"},
                "root_pane": {"pane_id": "evaluation-pane"}}

    async def agent_start(*args):
        observed["agent_args"] = args[2]
        return "eval-agent", {"agent_session": {"kind": "id", "agent": "codex"}}

    async def prompt(*args):
        return "settled", {"agent_session": {"kind": "id", "agent": "codex"}}

    async def capture(*args):
        current = coordinator.store.latest(gid)
        return {"state": "settled", "agent_response": json.dumps(
            report(current["manifest"])
        )}

    async def run(*args, **kwargs):
        assert args[:2] == ("workspace", "close")
        if close_unconfirmed:
            raise RuntimeError("Herdr close timeout")
        return (0, "", "")

    monkeypatch.setattr(runtime, "_ensure_herdr_server", fake_server)
    monkeypatch.setattr(runtime, "_json", workspace)
    monkeypatch.setattr(runtime, "_start_agent", agent_start)
    monkeypatch.setattr(runtime, "_prompt", prompt)
    monkeypatch.setattr(runtime, "_native_id", lambda *args: "native-eval-7")
    monkeypatch.setattr(runtime, "_capture", capture)
    monkeypatch.setattr(runtime, "_run", run)

    revision = graph_runtime.get_graph(gid)["revision"]
    entry = asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    eid = entry["evaluation_id"]
    assert observed["cwd"] != str(originals["backend"])
    assert not __import__("pathlib").Path(observed["cwd"]).exists()
    assert entry["raw_response"] is not None
    assert json.loads(entry["raw_response"])["verdict"] == "pass"
    assert entry["native_capture"]["state"] == "settled"
    assert entry["native_capture"]["response"] == entry["raw_response"]

    if not close_unconfirmed:
        assert entry["status"] == "passed"
        assert entry["cleanup"] is None
        assert show_claim(workspace=runtime.root, repository="backend")["claim_id"] is None
        assert coordinator.finalize(gid, eid, revision)["finalized_at_ns"]
        return

    # The capture says PASS, but worker close is unproven: only INTERRUPTED
    # is safe and get_graph/get_final_evaluation must surface operator locators.
    assert entry["status"] == "interrupted"
    assert entry["report"] is None
    assert entry["finalized_at_ns"] is None
    assert "worker termination" in entry["detail"]
    assert entry["cleanup"] == {
        "cleanup_state": "workspace_close_unconfirmed",
        "workspace_id": "preserved-native-workspace",
        "write_claim_id": "turn:" + entry["turn_id"],
        "write_claim_repository": "backend",
        "recovery_action": "Verify worker has stopped; then release claim",
    }
    assert coordinator.graph_status(gid)["final_evaluation_status"] == "interrupted"
    assert coordinator.graph_status(gid)["delivery_status"] == "not_finalized"

    reloaded = FinalEvaluationStore(runtime.db).get(eid)
    assert reloaded["cleanup"] == entry["cleanup"]
    assert reloaded["raw_response"] == entry["raw_response"]
    assert show_claim(workspace=runtime.root, repository="backend")[
        "claim_id"
    ] == reloaded["cleanup"]["write_claim_id"]

    with pytest.raises((RuntimeError, ValueError), match="PASS"):
        coordinator.finalize(gid, eid, revision)
    with pytest.raises(RuntimeError, match="operator"):
        asyncio.run(coordinator.start(gid, "codex-evaluator", revision))

    # Operator must confirm Herdr worker termination and release BOTH the
    # exact claim and the interrupted attempt, in that order.
    with pytest.raises(ValueError, match="worker has stopped"):
        release_stale_claim(
            workspace=runtime.root, repository="backend",
            claim_id=reloaded["cleanup"]["write_claim_id"],
            worker_termination_confirmed=False,
        )
    assert release_stale_claim(
        workspace=runtime.root, repository="backend",
        claim_id=reloaded["cleanup"]["write_claim_id"],
        worker_termination_confirmed=True,
    )["released"]
    with pytest.raises(RuntimeError, match="operator"):
        asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    recovered = recover_final_evaluation(
        workspace=runtime.root, evaluation_id=eid,
        worker_termination_confirmed=True,
    )
    assert recovered["status"] == "errored"
    assert recovered["cleanup"] == entry["cleanup"]  # retained for audit
    assert recovered["finalized_at_ns"] is None
    with sqlite3.connect(runtime.db) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM final_evaluation_recovery_audit "
            "WHERE evaluation_id=?", (eid,),
        ).fetchone()[0] == 1

    async def fresh_native_delegate(**kwargs):
        coordinator.store.bind_turn(kwargs["evaluation_id"], "native-eval-retry")
        snapshot_manifest = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        response = json.dumps(report(snapshot_manifest))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("native-eval-retry", "new-session", "backend",
                 "codex-evaluator", "settled", response, 987),
            )
        return {"turn_id": "native-eval-retry", "state": "settled",
                "agent_response": response}

    monkeypatch.setattr(runtime, "delegate", fresh_native_delegate)
    retried = asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    assert retried["status"] == "passed"
    assert retried["evaluation_id"] != eid


def test_crash_with_captured_pass_and_held_claim_never_auto_recovers_pass(tmp_path):
    """If process crashes between native capture and cleanup, held claim blocks PASS."""
    runtime, requests, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    binding = requests.graph_binding(gid)
    manifest = __import__("qiqi_delegate.final_eval_snapshot", fromlist=["inspect_roots"])
    content = manifest.inspect_roots(runtime.repos())
    eid_row, created = coordinator.store.reserve(
        gid, graph_runtime.get_graph(gid)["revision"],
        binding["request_id"], binding["request_revision"],
        "codex-evaluator", content, manifest.manifest_digest(content),
    )
    assert created
    eid = eid_row["evaluation_id"]
    coordinator.store.bind_turn(eid, "crash-captured")
    response = json.dumps(report(content))
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("crash-captured", "native-session", "backend",
             "codex-evaluator", "settled", response, 121),
        )
        db.execute(
            "UPDATE final_evaluations SET owner_pid=?, owner_start_token=? "
            "WHERE evaluation_id=?",
            (987654321, "nonexistent-owner", eid),
        )
    runtime._claim(
        "backend", "turn:crash-captured",
        repository_root=runtime.repos()["backend"],
    )
    restarted = FinalEvaluationStore(runtime.db)
    old = restarted.get(eid)
    assert old["status"] == "interrupted"
    assert old["raw_response"] == response
    assert old["native_capture"]["response"] == response
    assert "claim" in old["detail"]
    assert restarted.recover_abandoned() == 0
    with pytest.raises((RuntimeError, ValueError), match="PASS"):
        coordinator.finalize(gid, eid, graph_runtime.get_graph(gid)["revision"])


def test_legacy_store_schema_adds_cleanup_column_without_losing_attempts(tmp_path):
    db_path = tmp_path / "older-evaluations.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("""
            CREATE TABLE final_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                graph_run_id TEXT NOT NULL,
                graph_revision INTEGER NOT NULL,
                request_id TEXT NOT NULL,
                request_revision INTEGER NOT NULL,
                route TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                turn_id TEXT,
                raw_response TEXT,
                report_json TEXT,
                detail TEXT,
                owner_pid INTEGER NOT NULL,
                owner_start_token TEXT,
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL,
                finalized_at_ns INTEGER
            )
        """)
        db.execute(
            "INSERT INTO final_evaluations VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("old-eval", "old-graph", 1, "old-request", 1,
             "codex-evaluator", "{}", "deadbeef", "errored",
             None, None, None, "prior failure", 123, "old-token",
             1, 2, None),
        )
    store = FinalEvaluationStore(db_path)
    assert store.get("old-eval")["detail"] == "prior failure"
    assert store.get("old-eval")["cleanup"] is None
    with sqlite3.connect(db_path) as db:
        assert "cleanup_json" in {
            row[1] for row in db.execute(
                "PRAGMA table_info(final_evaluations)"
            ).fetchall()
        }
