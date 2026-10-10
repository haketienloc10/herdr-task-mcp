"""Review #5480005600: A->B->A delivery projection must match the live snapshot."""
import asyncio
import json
import sqlite3

from test_final_evaluation import report, setup_graph


def _native_dispatch(runtime, coordinator, calls):
    async def fake_delegate(**kwargs):
        eid = kwargs["evaluation_id"]
        calls.append(eid)
        turn_id = f"snapshot-native-{len(calls)}"
        coordinator.store.bind_turn(eid, turn_id)
        body = json.dumps(report(coordinator.store.get(eid)["manifest"]))
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                (turn_id, f"snapshot-session-{len(calls)}", "backend",
                 "codex-evaluator", "settled", body, len(calls)),
            )
        return {"state": "settled", "turn_id": turn_id, "agent_response": body}
    return fake_delegate


def test_finalized_a_then_finalized_b_then_revert_to_a_keeps_delivery(
    tmp_path, monkeypatch,
):
    """Regression: latest finalized B may be stale while older A is current."""
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    calls = []
    monkeypatch.setattr(runtime, "delegate", _native_dispatch(
        runtime, coordinator, calls,
    ))
    rev = graph_rt.get_graph(gid)["revision"]
    backend = runtime.repos()["backend"] / "orders.py"
    original = backend.read_bytes()

    a = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    coordinator.finalize(gid, a["evaluation_id"], rev)
    assert coordinator.graph_status(gid)["delivery_status"] == "finalized"

    backend.write_bytes(original + b"\n# delivery snapshot B\n")
    assert coordinator.graph_status(gid)["delivery_status"] == "not_finalized"
    b = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    assert b["evaluation_id"] != a["evaluation_id"]
    coordinator.finalize(gid, b["evaluation_id"], rev)
    at_b = coordinator.graph_status(gid)
    assert at_b["final_evaluation_id"] == b["evaluation_id"]
    assert at_b["delivery_status"] == "finalized"

    # Revert to the exact original content while keeping both historical
    # finalized evaluations intact. Newest finalized B must NOT mask A.
    backend.write_bytes(original)
    assert coordinator.store.latest(gid)["evaluation_id"] == b["evaluation_id"]
    assert coordinator.read(gid, b["evaluation_id"])["effective_status"] == "stale"
    assert coordinator.read(gid, a["evaluation_id"])["is_current"] is True
    at_a = coordinator.graph_status(gid)
    assert at_a["delivery_status"] == "finalized"
    assert at_a["final_evaluation_status"] == "passed"
    assert at_a["evaluation_is_current"] is True
    assert at_a["final_evaluation_id"] == a["evaluation_id"]

    reused = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    assert reused["evaluation_id"] == a["evaluation_id"]
    assert reused["already_finalized"] is True
    assert reused["is_current"] is True
    assert coordinator.graph_status(gid)["final_evaluation_id"] == a["evaluation_id"]
    assert len(calls) == 2
    with sqlite3.connect(runtime.db) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM final_evaluations WHERE graph_run_id=?",
            (gid,),
        ).fetchone()[0] == 2

    # A third unseen content digest still needs an entirely new evaluation.
    backend.write_bytes(original + b"\n# snapshot C\n")
    at_c = coordinator.graph_status(gid)
    assert at_c["delivery_status"] == "not_finalized"
    assert at_c["evaluation_is_current"] is False
    c = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    assert c["evaluation_id"] not in {
        a["evaluation_id"], b["evaluation_id"],
    }
    assert len(calls) == 3
    assert coordinator.graph_status(gid)["delivery_status"] == "not_finalized"


def test_a_b_a_revert_matches_only_identical_task_request_binding(
    tmp_path, monkeypatch,
):
    """An older PASS for another task request is not delivery authorization."""
    runtime, requests, graph_rt, coordinator, gid = setup_graph(tmp_path)
    calls = []
    monkeypatch.setattr(runtime, "delegate", _native_dispatch(
        runtime, coordinator, calls,
    ))
    rev = graph_rt.get_graph(gid)["revision"]
    original_binding = requests.graph_binding(gid)
    original_request_id = original_binding["request_id"]

    first = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    coordinator.finalize(gid, first["evaluation_id"], rev)
    assert coordinator.graph_status(gid)["delivery_status"] == "finalized"

    new_request = requests.create("Verify a different end-to-end API contract")
    assessed = requests.assess(new_request["request_id"], 1, {
        "requirements": [
            {"id": "R1", "text": "Validate new contract",
             "evidence_refs": ["request:current"]},
        ],
        "blocking_unknowns": [],
        "decision": "direct",
        "rationale": "New user request specifies new contract",
    })
    assert assessed["revision"] == requests.get(original_request_id)["revision"]
    # Public bind_graph() intentionally forbids switching Task Request IDs.
    # Simulate a persistence import/manual database edit in this *adversarial*
    # test only. The Final Gate must reject the historical PASS even if a
    # different request has an identical numeric revision and worktree hash.
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "UPDATE task_graph_bindings SET request_id=?, request_revision=? "
            "WHERE graph_run_id=?",
            (new_request["request_id"], assessed["revision"], gid),
        )
    # Same Git files, graph revision and request revision; different request ID.
    assert coordinator.read(gid, first["evaluation_id"])["is_current"] is False
    assert coordinator.graph_status(gid)["delivery_status"] == "not_finalized"
    newer = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    assert newer["evaluation_id"] != first["evaluation_id"]
    assert len(calls) == 2
    coordinator.finalize(gid, newer["evaluation_id"], rev)
    assert coordinator.graph_status(gid)["final_evaluation_id"] == newer["evaluation_id"]

    # Rebind original request; only its own original PASS is eligible again.
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "UPDATE task_graph_bindings SET request_id=?, request_revision=? "
            "WHERE graph_run_id=?",
            (original_request_id,
             requests.get(original_request_id)["revision"], gid),
        )
    status = coordinator.graph_status(gid)
    assert status["delivery_status"] == "finalized"
    assert status["final_evaluation_id"] == first["evaluation_id"]
    reused = asyncio.run(coordinator.start(gid, "codex-evaluator", rev))
    assert reused["already_finalized"] is True
    assert reused["evaluation_id"] == first["evaluation_id"]
    assert len(calls) == 2
