"""Durable TaskGraph semantics and retry intent across runtime process restarts."""
import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

from qiqi_delegate.task_graph_runtime import (
    GraphRuntime, decisions_from_payload, task_graph_from_payload,
)
from qiqi_delegate.task_graph_store import GraphRuntimeStore


def make_runtime(path):
    return GraphRuntime(GraphRuntimeStore(path))


def make_graph():
    return task_graph_from_payload({"nodes": [
        {
            "node_id": "B1", "repository": "backend", "route": "codex-balanced",
            "task_packet": {
                "objective": "Implement API",
                "scope": ["src/backend"],
                "acceptance_criteria": ["Tests pass"],
            },
        },
        {
            "node_id": "F1", "repository": "frontend", "route": "claude-balanced",
            "depends_on": ["B1"],
            "task_packet": {
                "objective": "Integrate API",
                "scope": ["src/frontend"],
                "acceptance_criteria": ["Frontend tests pass"],
            },
        },
    ]})


async def settled(node):
    return {
        "state": "settled",
        "session_id": "session_" + node.node_id,
        "turn_id": "turn_" + node.node_id,
        "agent_response": "Evidence for " + node.node_id,
    }


def test_graph_restores_idle_review_accept_and_dependency(tmp_path):
    path = tmp_path / "graph.sqlite3"
    graph = make_graph()
    original = make_runtime(path)
    start = original.start_graph(graph, repository_names={"backend", "frontend"})
    run_id = start["graph_run_id"]
    assert make_runtime(path).get_graph(run_id)["runnable_nodes"] == ["B1"]

    first = asyncio.run(make_runtime(path).delegate_next(run_id, executor=settled))
    item = first["review_required"][0]
    restarted = make_runtime(path)
    assert restarted.get_graph(run_id)["graph_state"] == "awaiting_review"
    batch = restarted.get_node_reviews(
        run_id, [("B1", item["attempt_id"])],
        expected_revision=first["revision"],
    )
    assert batch["reviews"][0]["result"]["agent_response"] == "Evidence for B1"

    accepted = restarted.submit_decisions(
        run_id, decisions_from_payload([{"node_id": "B1", "action": "accept"}]),
        expected_revision=first["revision"],
    )
    assert accepted["runnable_nodes"] == ["F1"]
    later = make_runtime(path)
    assert later.get_graph(run_id)["nodes"][0]["semantic_state"] == "satisfied"
    second = asyncio.run(later.delegate_next(run_id, executor=settled))
    done = make_runtime(path).submit_decisions(
        run_id, decisions_from_payload([{"node_id": "F1", "action": "accept"}]),
        expected_revision=second["revision"],
    )
    assert done["graph_state"] == "complete"
    assert make_runtime(path).get_graph(run_id)["graph_state"] == "complete"
    assert len(make_runtime(path).store.list_attempts(run_id, "B1")) == 1


def test_retry_plan_feedback_and_exact_resume_session_survive_restart(tmp_path):
    path = tmp_path / "graph.sqlite3"
    run_id = make_runtime(path).start_graph(
        make_graph(), repository_names={"backend", "frontend"},
    )["graph_run_id"]
    first = asyncio.run(make_runtime(path).delegate_next(run_id, executor=settled))
    decided = make_runtime(path).submit_decisions(
        run_id,
        decisions_from_payload([{
            "node_id": "B1", "action": "retry", "resume_session": True,
            "feedback": ["Add evidence from src/backend/api.py:42"],
        }]),
        expected_revision=first["revision"],
    )
    assert decided["runnable_nodes"] == ["B1"]

    restored = make_runtime(path)
    node = next(x for x in restored.get_graph(run_id)["nodes"] if x["node_id"] == "B1")
    assert node["retry_pending"] == {
        "resume_session": True,
        "session_id": "session_B1",
        "feedback": ["Add evidence from src/backend/api.py:42"],
    }
    calls = []

    async def start(_node):
        raise AssertionError("retry must resume native session")

    async def resume(node, session_id):
        calls.append((node.node_id, session_id, node.task_packet.as_dict()))
        return await settled(node)

    second = asyncio.run(restored.delegate_next(
        run_id, executor=start, resume_executor=resume,
    ))
    assert second["results"][0]["resume_session"] is True
    assert calls[0][1] == "session_B1"
    assert {
        "claim": "Add evidence from src/backend/api.py:42",
        "source": "QiQi semantic review",
    } in calls[0][2]["context"]["claims_to_investigate"]
    assert make_runtime(path).store.get_retry_plans(run_id) == {}
    assert make_runtime(path).get_graph(run_id)["graph_state"] == "awaiting_review"
    attempts = make_runtime(path).store.list_attempts(run_id, "B1")
    assert len(attempts) == 2
    assert attempts[1]["resume_session"] is True


def test_reconcile_persists_new_graph_and_clears_invalidated_retry(tmp_path):
    path = tmp_path / "graph.sqlite3"
    original = make_runtime(path)
    run_id = original.start_graph(
        make_graph(), repository_names={"backend", "frontend"},
    )["graph_run_id"]
    first = asyncio.run(original.delegate_next(run_id, executor=settled))
    planned = original.submit_decisions(
        run_id, decisions_from_payload([{
            "node_id": "B1", "action": "retry",
            "feedback": ["Missing acceptance proof"],
        }]),
        expected_revision=first["revision"],
    )
    graph_json = make_graph().as_dict()
    graph_json["nodes"][0]["task_packet"]["objective"] = "Implement corrected API"
    graph_json["nodes"].append({
        "node_id": "D1", "repository": "docs", "route": "codex-balanced",
        "depends_on": ["F1"],
        "task_packet": {
            "objective": "Document the change", "scope": ["docs"],
            "acceptance_criteria": ["Documentation complete"],
        },
    })
    replacement = task_graph_from_payload(graph_json)
    updated = make_runtime(path).reconcile_graph(
        run_id, replacement, repository_names={"backend", "frontend", "docs"},
        expected_revision=planned["revision"],
    )
    assert "B1" in updated["reconciliation"]["changed_nodes"]
    assert "F1" in updated["reconciliation"]["dependency_invalidated_nodes"]
    reloaded = make_runtime(path)
    assert reloaded.get_graph(run_id)["authored_node_count"] == 3
    assert reloaded.store.get_retry_plans(run_id) == {}
    assert reloaded.store.load_graph(run_id) == replacement
    assert reloaded.get_graph(run_id)["runnable_nodes"] == ["B1"]


def test_restart_fails_closed_for_running_wave_and_preserves_attempt(tmp_path):
    path = tmp_path / "graph.sqlite3"
    original = make_runtime(path)
    run_id = original.start_graph(
        make_graph(), repository_names={"backend", "frontend"},
    )["graph_run_id"]
    attempt_id = original.store.start_attempt(run_id, "B1", "wave-interrupted")
    restarted = make_runtime(path)
    state = restarted.get_graph(run_id)
    assert state["graph_state"] == "running"
    assert state["current_wave_id"] == "wave-interrupted"
    assert state["nodes"][0]["current_attempt_id"] == attempt_id
    with pytest.raises(RuntimeError, match="not ready"):
        asyncio.run(restarted.delegate_next(run_id, executor=settled))
    assert restarted.store.get_attempt(attempt_id)["runtime_state"] == "running"
    assert len(restarted.store.list_attempts(run_id, "B1")) == 1


def test_restart_finishes_quiescent_wave_without_restarting_attempt(tmp_path):
    """Crash after attempt finishes but before close_wave does not strand the graph."""
    path = tmp_path / "graph.sqlite3"
    original = make_runtime(path)
    run_id = original.start_graph(
        make_graph(), repository_names={"backend", "frontend"},
    )["graph_run_id"]
    attempt_id = original.store.start_attempt(run_id, "B1", "wave-finished")
    original.store.finish_attempt(
        attempt_id, runtime_state="settled",
        result={"state": "settled", "agent_response": "Persisted Peer result"},
        session_id="session_B1", turn_id="turn_B1",
    )
    before = original.store.get_run(run_id)
    assert before["current_wave_id"] == "wave-finished"

    restarted = make_runtime(path)
    current = restarted.get_graph(run_id)
    assert current["graph_state"] == "awaiting_review"
    assert current["current_wave_id"] is None
    assert current["revision"] == before["revision"] + 1
    assert len(restarted.store.list_attempts(run_id, "B1")) == 1
    assert restarted.get_node_reviews(
        run_id, [("B1", attempt_id)]
    )["reviews"][0]["result"]["agent_response"] == "Persisted Peer result"
    # Idempotent if a surviving caller tries to close the same wave.
    restarted.store.close_wave(run_id, "wave-finished")
    assert restarted.get_graph(run_id)["revision"] == current["revision"]


def independent_graph():
    payload = make_graph().as_dict()
    payload["nodes"][1].pop("depends_on")
    return task_graph_from_payload(payload)


def test_concurrent_reconcile_is_rejected_before_stale_wave_dispatch(tmp_path, monkeypatch):
    """No stale authored route/packet may execute after another server commits a DAG."""
    path = tmp_path / "graph.sqlite3"
    runtime_a = make_runtime(path)
    original = independent_graph()
    run_id = runtime_a.start_graph(
        original, repository_names={"backend", "frontend"},
    )["graph_run_id"]
    runtime_b = make_runtime(path)
    replacement_payload = original.as_dict()
    replacement_payload["nodes"][0]["task_packet"]["objective"] = "New authoritative API"
    replacement = task_graph_from_payload(replacement_payload)
    select = runtime_a._select_wave_nodes

    def reconcile_between_snapshot_and_claim(graph_run_id, candidates):
        selected = select(graph_run_id, candidates)
        revision = runtime_b.get_graph(run_id)["revision"]
        runtime_b.reconcile_graph(
            run_id, replacement,
            repository_names={"backend", "frontend"},
            expected_revision=revision,
        )
        return selected

    monkeypatch.setattr(runtime_a, "_select_wave_nodes", reconcile_between_snapshot_and_claim)
    executions = []

    async def run_worker(node):
        executions.append(node.node_id)
        return await settled(node)

    with pytest.raises(RuntimeError, match="stale graph snapshot revision"):
        asyncio.run(runtime_a.delegate_next(run_id, executor=run_worker))
    assert executions == []
    assert runtime_b.store.load_graph(run_id) == replacement
    assert runtime_b.store.get_run(run_id)["current_wave_id"] is None
    assert runtime_b.store.list_attempts(run_id, "B1") == []
    assert runtime_b.store.list_attempts(run_id, "F1") == []


def test_multi_retry_wave_claim_rolls_back_if_second_node_fails(tmp_path):
    """Failure injecting the second INSERT never loses the first node's retry plan."""
    path = tmp_path / "graph.sqlite3"
    runtime = make_runtime(path)
    run_id = runtime.start_graph(
        independent_graph(), repository_names={"backend", "frontend"},
    )["graph_run_id"]
    first = asyncio.run(runtime.delegate_next(run_id, executor=settled))
    assert {x["node_id"] for x in first["results"]} == {"B1", "F1"}
    decided = runtime.submit_decisions(
        run_id,
        decisions_from_payload([
            {"node_id": "B1", "action": "retry", "feedback": ["Check API tests"]},
            {"node_id": "F1", "action": "retry", "feedback": ["Check UI tests"]},
        ]),
        expected_revision=first["revision"],
    )
    assert set(decided["runnable_nodes"]) == {"B1", "F1"}
    expected_plans = runtime.store.get_retry_plans(run_id)
    assert set(expected_plans) == {"B1", "F1"}
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TRIGGER fail_second_wave_node
            BEFORE INSERT ON graph_attempts
            WHEN NEW.node_id = 'F1' AND NEW.attempt_number = 2
            BEGIN SELECT RAISE(ABORT, 'injected failure on second claim'); END;
        """)
    executions = []

    async def run_worker(node):
        executions.append(node.node_id)
        return await settled(node)

    with pytest.raises(sqlite3.IntegrityError, match="second claim"):
        asyncio.run(make_runtime(path).delegate_next(run_id, executor=run_worker))
    assert executions == []
    restored = make_runtime(path)
    assert restored.store.get_retry_plans(run_id) == expected_plans
    assert restored.store.get_run(run_id)["current_wave_id"] is None
    assert restored.get_graph(run_id)["graph_state"] == "ready"
    for node_id in ("B1", "F1"):
        assert len(restored.store.list_attempts(run_id, node_id)) == 1
        assert restored.get_graph(run_id)["nodes"][0 if node_id == "B1" else 1]["retry_pending"]

    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER fail_second_wave_node")
    result = asyncio.run(make_runtime(path).delegate_next(run_id, executor=run_worker))
    assert set(executions) == {"B1", "F1"}
    assert {x["node_id"] for x in result["results"]} == {"B1", "F1"}
    assert restored.store.get_retry_plans(run_id) == {}
    assert all(
        restored.store.list_attempts(run_id, n)[-1]["dispatch_state"] == "dispatched"
        for n in ("B1", "F1")
    )


def test_prepared_restart_retains_retry_plan_and_fails_closed(tmp_path):
    """Crash after atomic claim but before dispatch cannot delete retry context."""
    path = tmp_path / "graph.sqlite3"
    runtime = make_runtime(path)
    run_id = runtime.start_graph(
        independent_graph(), repository_names={"backend", "frontend"},
    )["graph_run_id"]
    first = asyncio.run(runtime.delegate_next(run_id, executor=settled))
    decided = runtime.submit_decisions(
        run_id,
        decisions_from_payload([{
            "node_id": "B1", "action": "retry", "resume_session": True,
            "feedback": ["Rerun exactly in native session"],
        }]),
        expected_revision=first["revision"],
    )
    plan = runtime.store.get_retry_plans(run_id)["B1"]
    (attempt_id,) = runtime.store.start_wave(
        run_id, "pre-dispatch-crash",
        expected_revision=decided["revision"],
        attempts=({
            "node_id": "B1", "resume_session": True,
            "session_id": "session_B1", "retry_plan": plan,
        },),
    )
    fresh_process = make_runtime(path)
    assert fresh_process.get_graph(run_id)["graph_state"] == "running"
    assert fresh_process.store.get_retry_plans(run_id)["B1"] == plan
    attempt = fresh_process.store.get_attempt(attempt_id)
    assert attempt["runtime_state"] == "running"
    assert attempt["dispatch_state"] == "prepared"
    assert attempt["retry_plan_json"] is not None
    with pytest.raises(RuntimeError, match="not ready"):
        asyncio.run(fresh_process.delegate_next(run_id, executor=settled))
    # Dispatch transition is durable and consumes the pending plan exactly once.
    fresh_process.store.mark_attempt_dispatched(attempt_id)
    assert fresh_process.store.get_retry_plans(run_id) == {}
    after = fresh_process.store.get_attempt(attempt_id)
    assert after["dispatch_state"] == "dispatched"
    assert after["retry_plan_json"] == attempt["retry_plan_json"]
    with pytest.raises(RuntimeError, match="already dispatched"):
        fresh_process.store.mark_attempt_dispatched(attempt_id)


def test_concurrent_legacy_schema_migration_preserves_existing_attempts(tmp_path):
    """Concurrent servers opening a pre-upgrade database must not ALTER the same column."""
    path = tmp_path / "graph.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE graph_runs (
                graph_run_id TEXT PRIMARY KEY,
                graph_fingerprint TEXT NOT NULL,
                current_wave_id TEXT,
                revision INTEGER NOT NULL,
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL
            );
            CREATE TABLE graph_node_states (
                graph_run_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                semantic_state TEXT NOT NULL,
                runtime_state TEXT NOT NULL,
                current_attempt_id TEXT,
                session_id TEXT,
                turn_id TEXT,
                updated_at_ns INTEGER NOT NULL,
                PRIMARY KEY (graph_run_id, node_id)
            );
            CREATE TABLE graph_attempts (
                attempt_id TEXT PRIMARY KEY,
                graph_run_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                wave_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                runtime_state TEXT NOT NULL,
                resume_session INTEGER NOT NULL,
                session_id TEXT,
                turn_id TEXT,
                result_json TEXT,
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL
            );
            INSERT INTO graph_runs VALUES ('legacy', 'original', 'wave-old', 5, 1, 1);
            INSERT INTO graph_node_states VALUES
                ('legacy', 'B1', 'pending', 'running', 'attempt-old', NULL, NULL, 1);
            INSERT INTO graph_attempts VALUES
                ('attempt-old', 'legacy', 'B1', 'wave-old', 1,
                 'running', 0, NULL, NULL, NULL, 1, 1);
        """)

    workers = 8
    barrier = Barrier(workers)

    def open_concurrent_store(_):
        barrier.wait(timeout=15)
        with closing(GraphRuntimeStore(path)._connect()) as conn:
            return (
                conn.execute(
                    "SELECT dispatch_state FROM graph_attempts "
                    "WHERE attempt_id = 'attempt-old'"
                ).fetchone()[0],
                conn.execute(
                    "SELECT active FROM graph_node_states "
                    "WHERE graph_run_id = 'legacy' AND node_id = 'B1'"
                ).fetchone()[0],
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(open_concurrent_store, range(workers)))

    assert results == [("dispatched", 1)] * workers
    with sqlite3.connect(path) as conn:
        expected = {
            "graph_runs": {"graph_json"},
            "graph_node_states": {"active"},
            "graph_attempts": {"retry_plan_json", "dispatch_state"},
        }
        for table, additions in expected.items():
            names = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            for column in additions:
                assert names.count(column) == 1
        assert conn.execute(
            "SELECT current_wave_id, revision FROM graph_runs WHERE graph_run_id = 'legacy'"
        ).fetchone() == ("wave-old", 5)

    # All columns now exist; opening another server is an idempotent read.
    with closing(GraphRuntimeStore(path)._connect()) as conn:
        assert conn.execute(
            "SELECT retry_plan_json FROM graph_attempts WHERE attempt_id='attempt-old'"
        ).fetchone()[0] is None


def test_block_and_replan_defer_metadata_survive_restart_without_peer_turn(tmp_path):
    """Uncaptured startup failure still has an auditable Lead owner and checkpoint."""
    path = tmp_path / "graph.sqlite3"
    for action in ("block", "replan"):
        gr = make_runtime(path)
        started = gr.start_graph(
            task_graph_from_payload({"nodes": [{
                "node_id": "B1", "repository": "backend", "route": "codex-balanced",
                "task_packet": {
                    "objective": "Inspect", "scope": ["src"],
                    "acceptance_criteria": ["Explain startup"],
                },
            }]}), repository_names={"backend"},
        )
        run = started["graph_run_id"]

        async def fail(_node):
            raise RuntimeError("agent_not_ready before any captured turn")

        with pytest.raises(RuntimeError, match="agent_not_ready"):
            asyncio.run(gr.delegate_next(run, executor=fail))
        review_state = gr.get_graph(run)
        node_state = review_state["nodes"][0]
        assert node_state["turn_id"] is None
        attempt = node_state["current_attempt_id"]
        choice = decisions_from_payload([{
            "node_id": "B1", "action": action,
            "owner": "platform-oncall",
            "return_checkpoint": "after Herdr worker termination",
        }])
        decided = gr.submit_decisions(
            run, choice, expected_revision=review_state["revision"],
        )
        assert decided["decision_outcomes"][0]["defer"]["owner"] == "platform-oncall"
        restored = make_runtime(path)
        persisted = restored.get_graph(run)["nodes"][0]["last_lead_decision"]
        assert persisted["action"] == action
        assert persisted["owner"] == "platform-oncall"
        assert persisted["return_checkpoint"] == "after Herdr worker termination"
        assert persisted["attempt_id"] == attempt
        assert persisted["turn_id"] is None
        with sqlite3.connect(path) as db:
            row = db.execute(
                "SELECT turn_id, owner, return_checkpoint FROM lead_decisions "
                "WHERE graph_run_id = ? ORDER BY id DESC LIMIT 1", (run,),
            ).fetchone()
            assert row == (None, "platform-oncall", "after Herdr worker termination")


def test_legacy_decision_audit_migrates_with_preserved_rows(tmp_path):
    """Upgrading the old NOT NULL turn schema must not lose decision history."""
    path = tmp_path / "graph.sqlite3"
    gr = make_runtime(path)
    started = gr.start_graph(
        task_graph_from_payload({"nodes": [{
            "node_id": "B1", "repository": "backend", "route": "codex-balanced",
            "task_packet": {"objective": "Inspect", "scope": ["src"],
                            "acceptance_criteria": ["Evidence"]},
        }]}), repository_names={"backend"},
    )
    run = started["graph_run_id"]
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE lead_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            graph_run_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            node_id TEXT,
            attempt_id TEXT,
            created_at_ns INTEGER NOT NULL
        )""")
        db.execute(
            "INSERT INTO lead_decisions("
            "graph_run_id,turn_id,action,reason,node_id,attempt_id,created_at_ns"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run, "turn:old", "retry", "historic", "B1", "old-attempt", 10),
        )
    async def fail(_node):
        raise RuntimeError("agent_not_ready")

    with pytest.raises(RuntimeError, match="agent_not_ready"):
        asyncio.run(gr.delegate_next(run, executor=fail))
    current = gr.get_graph(run)
    gr.submit_decisions(
        run, decisions_from_payload([{
            "node_id": "B1", "action": "block", "owner": "oncall",
            "return_checkpoint": "manual release verified",
        }]), expected_revision=current["revision"],
    )
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT turn_id, action, owner, return_checkpoint "
            "FROM lead_decisions WHERE graph_run_id = ? ORDER BY id", (run,),
        ).fetchall()
        assert rows == [
            ("turn:old", "retry", None, None),
            (None, "block", "oncall", "manual release verified"),
        ]


def test_corrupt_or_legacy_graph_definition_fails_closed(tmp_path):
    path = tmp_path / "graph.sqlite3"
    runtime = make_runtime(path)
    run_id = runtime.start_graph(
        make_graph(), repository_names={"backend", "frontend"},
    )["graph_run_id"]
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE graph_runs SET graph_json = NULL WHERE graph_run_id = ?", (run_id,))
    with pytest.raises(RuntimeError, match="legacy run"):
        make_runtime(path).get_graph(run_id)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE graph_runs SET graph_json = ? WHERE graph_run_id = ?",
            ('{"nodes":[]}', run_id),
        )
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        make_runtime(path).get_graph(run_id)

def test_direct_runtime_cannot_accept_failed_peer_with_fake_turn(tmp_path):
    """Python callers cannot bypass the MCP's native capture ACCEPT guard."""
    graph = make_graph()
    gr = make_runtime(tmp_path / "graph.sqlite3")
    started = gr.start_graph(graph, repository_names={"backend", "frontend"})
    run_id = started["graph_run_id"]

    async def failed_with_turn(node):
        return {
            "state": "failed",
            "session_id": "native_" + node.node_id,
            "turn_id": "turn_" + node.node_id,
            "agent_response": "A failure is not successful evidence",
        }

    current = asyncio.run(gr.delegate_next(run_id, executor=failed_with_turn))
    assert current["graph_state"] == "awaiting_review"
    with pytest.raises(ValueError, match="exact settled attempt"):
        gr.submit_decisions(
            run_id,
            decisions_from_payload([{"node_id": "B1", "action": "accept"}]),
            expected_revision=current["revision"],
        )
    after = make_runtime(tmp_path / "graph.sqlite3").get_graph(run_id)
    assert after["graph_state"] == "awaiting_review"
    assert after["runnable_nodes"] == []
    assert after["nodes"][0]["semantic_state"] == "pending"
    assert after["nodes"][1]["semantic_state"] == "pending"


def test_graph_wave_conflicts_use_git_root_identity_not_mutable_repo_alias(tmp_path):
    """Two historic logical names mapped to one Git root never share a wave."""
    graph = task_graph_from_payload({"nodes": [{
        "node_id": name, "repository": name, "route": "codex-balanced",
        "task_packet": {
            "objective": name, "scope": ["src"],
            "acceptance_criteria": ["Evidence"],
        },
    } for name in ("old", "renamed")]})
    gr = GraphRuntime(
        GraphRuntimeStore(tmp_path / "graph.sqlite3"),
        repository_key=lambda name: "/canonical/same-root",
    )
    run_id = gr.start_graph(
        graph, repository_names={"old", "renamed"},
    )["graph_run_id"]
    work = []

    async def execute(node):
        work.append(node.node_id)
        return await settled(node)

    result = asyncio.run(gr.delegate_next(run_id, executor=execute))
    assert len(result["results"]) == 1
    assert len(work) == 1


