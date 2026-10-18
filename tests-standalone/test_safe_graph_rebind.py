"""Regression coverage for authored graph recovery and fail-closed safe rebind."""
import json
import sqlite3

import pytest

from test_final_evaluation import setup_graph
from qiqi_delegate.task_request import TaskRequestStore


def _binding_data(requests, graph_id):
    bound = requests.graph_binding(graph_id)
    req = requests.get(bound["request_id"])
    return bound, req


def _revision_only_reassessment(requests, graph_id):
    bound, req = _binding_data(requests, graph_id)
    return requests.assess(
        bound["request_id"], req["revision"], req["assessment"],
    )


def _snapshot(runtime, graph_id):
    with sqlite3.connect(runtime.db) as db:
        return db.execute(
            "SELECT revision, graph_json FROM graph_runs WHERE graph_run_id=?",
            (graph_id,),
        ).fetchone(), db.execute(
            "SELECT node_id, semantic_state, runtime_state, turn_id "
            "FROM graph_node_states WHERE graph_run_id=? ORDER BY node_id",
            (graph_id,),
        ).fetchall()


def test_get_graph_definition_recovers_exact_authored_packets_after_restart(tmp_path):
    runtime, requests, gr, _coordinator, gid = setup_graph(tmp_path)
    initial = gr._graph_for_run(gid).as_dict()
    before = _snapshot(runtime, gid)
    restored = gr.store.get_graph_definition(gid)
    assert restored == {
        "graph_run_id": gid, "revision": gr.get_graph(gid)["revision"],
        "graph": initial,
    }
    assert restored["graph"]["nodes"][1]["depends_on"] == ["backend"]
    assert restored["graph"]["nodes"][1]["task_packet"]["objective"] == "Display total"
    assert "scope" in restored["graph"]["nodes"][0]["task_packet"]
    assert restored["graph"]["nodes"][0]["task_packet"] == initial["nodes"][0]["task_packet"]
    assert _snapshot(runtime, gid) == before

    # A new process/store can read the persisted graph without chat context.
    from qiqi_delegate.task_graph_store import GraphRuntimeStore
    reloaded = GraphRuntimeStore(runtime.db)
    assert reloaded.get_graph_definition(gid) == restored


def test_exact_context_rebind_preserves_accept_and_requires_fresh_eval(tmp_path):
    runtime, requests, gr, evaluator, gid = setup_graph(tmp_path)
    bound, req = _binding_data(requests, gid)
    revision = gr.get_graph(gid)["revision"]
    before = _snapshot(runtime, gid)
    updated = _revision_only_reassessment(requests, gid)
    assert updated["revision"] == req["revision"] + 1
    assert evaluator.graph_status(gid)["delivery_status"] == "not_finalized"
    with pytest.raises(ValueError, match="stale"):
        evaluator._eligible(gid, revision)

    result = requests.rebind_graph(gid, revision, updated["revision"])
    assert result["preserved_nodes"] == ["backend", "frontend"]
    assert result["previous_request_revision"] == req["revision"]
    assert requests.graph_binding(gid)["request_revision"] == updated["revision"]
    assert gr.get_graph(gid)["revision"] == revision
    assert _snapshot(runtime, gid) == before
    assert evaluator._eligible(gid, revision)[3]["revision"] == updated["revision"]
    with sqlite3.connect(runtime.db) as db:
        audit = db.execute(
            "SELECT previous_revision, new_revision FROM task_graph_rebind_audit "
            "WHERE graph_run_id=?", (gid,),
        ).fetchall()
    assert audit == [(req["revision"], updated["revision"])]


@pytest.mark.parametrize("mutation", ["rationale", "requirement", "new_source"])
def test_rebind_refuses_changed_context_without_touching_accept(tmp_path, mutation):
    runtime, requests, gr, _eval, gid = setup_graph(tmp_path)
    bound, req = _binding_data(requests, gid)
    original = req["assessment"]
    if mutation == "new_source":
        added = requests.append(
            bound["request_id"], req["revision"],
            {"kind": "inline", "text": "New context after ACCEPT"},
        )
        updated = requests.assess(
            bound["request_id"], added["revision"], original,
        )
    else:
        changed = json.loads(json.dumps(original))
        if mutation == "rationale":
            changed["rationale"] = "Updated decision rationale"
        else:
            changed["requirements"][0]["text"] = "Different contract"
        updated = requests.assess(
            bound["request_id"], req["revision"], changed,
        )

    before = _snapshot(runtime, gid)
    with pytest.raises(ValueError, match="content differs"):
        requests.rebind_graph(gid, gr.get_graph(gid)["revision"], updated["revision"])
    assert requests.graph_binding(gid) == bound
    assert _snapshot(runtime, gid) == before
    with sqlite3.connect(runtime.db) as db:
        assert db.execute(
            "SELECT count(*) FROM task_graph_rebind_audit WHERE graph_run_id=?",
            (gid,),
        ).fetchone()[0] == 0


def test_legacy_binding_has_no_provable_context_and_fails_closed(tmp_path):
    runtime, requests, gr, _eval, gid = setup_graph(tmp_path)
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "DELETE FROM task_graph_binding_snapshots WHERE graph_run_id=?", (gid,),
        )
    next_request = _revision_only_reassessment(requests, gid)
    before = _snapshot(runtime, gid)
    with pytest.raises(ValueError, match="no trustworthy baseline"):
        requests.rebind_graph(gid, gr.get_graph(gid)["revision"], next_request["revision"])
    assert _snapshot(runtime, gid) == before


@pytest.mark.parametrize("change", ["stale_graph_revision", "running_wave", "node_not_accepted"])
def test_rebind_refuses_non_quiescent_or_incomplete_graph(tmp_path, change):
    runtime, requests, gr, _eval, gid = setup_graph(tmp_path)
    updated = _revision_only_reassessment(requests, gid)
    revision = gr.get_graph(gid)["revision"]
    with sqlite3.connect(runtime.db) as db:
        if change == "running_wave":
            db.execute(
                "UPDATE graph_runs SET current_wave_id='wave-test' "
                "WHERE graph_run_id=?", (gid,),
            )
        if change == "node_not_accepted":
            db.execute(
                "UPDATE graph_node_states SET semantic_state='pending' "
                "WHERE graph_run_id=? AND node_id='backend'", (gid,),
            )
    with pytest.raises((RuntimeError, ValueError), match="stale graph|wave is active|satisfied"):
        requests.rebind_graph(
            gid, revision - 1 if change == "stale_graph_revision" else revision,
            updated["revision"],
        )
    assert requests.graph_binding(gid)["request_revision"] == updated["revision"] - 1
