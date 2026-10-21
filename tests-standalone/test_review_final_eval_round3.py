"""Review #5479500455: full task evidence, preserved workers, gitlinks and finalize races."""
import asyncio
import json
import sqlite3
import subprocess

import pytest

from qiqi_delegate.final_eval_manifest import inspect_roots, manifest_digest
from qiqi_delegate.runtime import AgentStartupBlocked
from test_final_evaluation import setup_graph, report


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        capture_output=True, text=True,
    ).stdout


def _task_with_sources(requests, graph_runtime, gid, sources):
    binding = requests.graph_binding(gid)
    rid = binding["request_id"]
    revision = requests.get(rid)["revision"]
    for source in sources:
        current = requests.append(rid, revision, source)
        revision = current["revision"]
    refs = [s["id"] for s in current["sources"]]
    assessed = requests.assess(rid, revision, {
        "requirements": [{
            "id": "R1",
            "text": "Implement attached specification, not merely Lead summary",
            "evidence_refs": ["request:current", *refs],
        }],
        "blocking_unknowns": [],
        "decision": "direct",
        "rationale": "Full specification supplied as source",
    })
    requests.bind_graph(
        gid, rid, assessed["revision"],
        list(binding["requirement_map"]),
        binding["requirement_map"],
        replace=True,
    )
    return assessed



def test_preserved_agent_startup_is_interrupted_and_requires_operator(tmp_path, monkeypatch):
    runtime, requests, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    async def blocked_delegate(**kwargs):
        # Native launch already bound turn ID before discovering that the
        # startup UI blocked; runtime deliberately preserves its worker.
        coordinator.store.bind_turn(kwargs["evaluation_id"], "blocked-native-turn")
        raise AgentStartupBlocked(
            "codex", "pane-123", "operator approval required",
        )
    monkeypatch.setattr(runtime, "delegate", blocked_delegate)
    revision = graph_runtime.get_graph(gid)["revision"]
    with pytest.raises(AgentStartupBlocked):
        asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    first = coordinator.store.latest(gid)
    assert first["status"] == "interrupted"
    assert first["turn_id"] == "blocked-native-turn"
    with pytest.raises(RuntimeError, match="operator"):
        asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    coordinator.store.release_interrupted(
        first["evaluation_id"], worker_termination_confirmed=True,
    )
    # Once the operator confirms the external Herdr worker has stopped, a
    # new attempt can reserve safely.
    async def clean_delegate(**kwargs):
        coordinator.store.bind_turn(kwargs["evaluation_id"], "retry-native")
        manifest = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        body = json.dumps(report(manifest))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("retry-native", "native-session", "backend",
                 "codex-evaluator", "settled", body, 55),
            )
        return {"state": "settled", "turn_id": "retry-native",
                "agent_response": body}
    monkeypatch.setattr(runtime, "delegate", clean_delegate)
    assert asyncio.run(
        coordinator.start(gid, "codex-evaluator", revision)
    )["status"] == "passed"



def test_finalization_revoked_if_source_changes_during_commit(tmp_path, monkeypatch):
    runtime, requests, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    binding = requests.graph_binding(gid)
    manifest = inspect_roots(runtime.repos())
    rev = graph_runtime.get_graph(gid)["revision"]
    reserved, created = coordinator.store.reserve(
        gid, rev, binding["request_id"], binding["request_revision"],
        "codex-evaluator", manifest, manifest_digest(manifest),
    )
    assert created
    eid = reserved["evaluation_id"]
    coordinator.store.bind_turn(eid, "finalization-race-turn")
    body = json.dumps(report(manifest))
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("finalization-race-turn", "native-session", "backend",
             "codex-evaluator", "settled", body, 99),
        )
    coordinator.store.complete(
        eid, raw_response=body, report=report(manifest),
        status="passed", turn_id="finalization-race-turn",
    )
    original_finalize = coordinator.store.finalize
    rootfile = runtime.repos()["backend"] / "orders.py"

    def mutate_after_commit(*args, **kwargs):
        persisted = original_finalize(*args, **kwargs)
        # Simulate a Peer/worktree writer changing the deliverable precisely
        # after the SQLite finalization was committed.
        rootfile.write_text(rootfile.read_text() + "\n# concurrent mutation\n")
        return persisted

    monkeypatch.setattr(coordinator.store, "finalize", mutate_after_commit)
    with pytest.raises(RuntimeError, match="delivery was revoked"):
        coordinator.finalize(gid, eid, rev)
    row = coordinator.store.get(eid)
    assert row["status"] == "passed"
    assert row["finalized_at_ns"] is None
    assert coordinator.graph_status(gid)["delivery_status"] == "not_finalized"
    assert coordinator.read(gid)["effective_status"] == "stale"
    with sqlite3.connect(runtime.db) as db:
        audit = db.execute(
            "SELECT previous_finalized_at_ns, reason "
            "FROM final_evaluation_revocations WHERE evaluation_id=?",
            (eid,),
        ).fetchone()
    assert audit is not None
    assert audit[0] > 0 and "changed during finalization" in audit[1]
