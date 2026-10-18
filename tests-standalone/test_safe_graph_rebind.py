"""Regression coverage for authored graph recovery and fail-closed safe rebind."""
import json
import sqlite3

import pytest

from test_final_evaluation import setup_graph
from qiqi_delegate.task_graph_runtime import task_graph_from_payload


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


@pytest.mark.parametrize("damage", ["missing", "mismatched"])
def test_get_graph_definition_fails_closed_on_legacy_or_tampered_data(tmp_path, damage):
    runtime, _requests, gr, _eval, gid = setup_graph(tmp_path)
    with sqlite3.connect(runtime.db) as db:
        if damage == "missing":
            db.execute(
                "UPDATE graph_runs SET graph_json=NULL WHERE graph_run_id=?", (gid,),
            )
        else:
            db.execute(
                "UPDATE graph_runs SET graph_json=? WHERE graph_run_id=?",
                (json.dumps({"nodes": []}), gid),
            )
    with pytest.raises(RuntimeError, match="unavailable|fingerprint mismatch"):
        gr.store.get_graph_definition(gid)


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
            gid, revision + 1 if change == "stale_graph_revision" else revision,
            updated["revision"],
        )
    assert requests.graph_binding(gid)["request_revision"] == updated["revision"] - 1


def _graph_with_unreferenced_file_source(tmp_path):
    runtime, requests, gr, _evaluator, gid = setup_graph(tmp_path, with_request=False)
    source_file = runtime.repos()["backend"] / "contract.txt"
    source_file.write_text("baseline contract", encoding="utf-8")
    req = requests.create(
        "Verify cross-repo contract",
        sources=[{"kind": "repo_file", "repository": "backend",
                  "path": "contract.txt"}],
    )
    assessed = requests.assess(req["request_id"], 1, {
        "requirements": [
            {"id": "R1", "text": "Check behavior",
             "evidence_refs": ["request:current"]},
        ],
        "blocking_unknowns": [], "decision": "direct",
        "rationale": "Request specifies verification",
    })
    requests.bind_graph(
        gid, req["request_id"], assessed["revision"],
        ["backend", "frontend"], {"backend": ["R1"], "frontend": ["R1"]},
    )
    return runtime, requests, gr, gid, source_file


def test_rebind_rejects_unreferenced_stale_file_source(tmp_path):
    runtime, requests, gr, gid, source_file = _graph_with_unreferenced_file_source(tmp_path)
    updated = _revision_only_reassessment(requests, gid)
    source_file.write_text("changed after review", encoding="utf-8")
    assert requests.assert_ready(updated["request_id"])["stale_sources"]
    before = _snapshot(runtime, gid)
    with pytest.raises(ValueError, match="stale context sources"):
        requests.rebind_graph(gid, gr.get_graph(gid)["revision"], updated["revision"])
    assert _snapshot(runtime, gid) == before


def test_rebind_rechecks_file_source_inside_write_transaction(tmp_path, monkeypatch):
    runtime, requests, gr, gid, source_file = _graph_with_unreferenced_file_source(tmp_path)
    updated = _revision_only_reassessment(requests, gid)
    original = requests._stale_source_ids
    checks = []

    def changed_during_lock(sources):
        checks.append(True)
        source_file.write_text("changed after optimistic preflight", encoding="utf-8")
        return original(sources)

    monkeypatch.setattr(requests, "_stale_source_ids", changed_during_lock)
    before = _snapshot(runtime, gid)
    with pytest.raises(ValueError, match="stale context sources"):
        requests.rebind_graph(gid, gr.get_graph(gid)["revision"], updated["revision"])
    assert len(checks) == 1
    assert _snapshot(runtime, gid) == before


def _revised_graph(gr, gid):
    original = gr._graph_for_run(gid).as_dict()
    original["nodes"][0]["task_packet"]["objective"] = "Revised backend implementation"
    return task_graph_from_payload(original)


@pytest.mark.parametrize("failure", ["request_advanced", "wrong_bound_revision"])
def test_reconciliation_rolls_back_graph_when_binding_fails(tmp_path, failure):
    runtime, requests, gr, _eval, gid = setup_graph(tmp_path)
    binding, req = _binding_data(requests, gid)
    before = _snapshot(runtime, gid)
    current_revision = gr.get_graph(gid)["revision"]
    if failure == "request_advanced":
        # Simulates a concurrent reassessment after server preflight but
        # before the graph-store transaction's binding callback.
        _revision_only_reassessment(requests, gid)
    else:
        assert req["revision"] == binding["request_revision"]

    def commit_binding(conn):
        requests.bind_graph(
            gid, binding["request_id"], req["revision"],
            ["backend", "frontend"], binding["requirement_map"],
            replace=True, connection=conn,
            expected_bound_revision=(
                binding["request_revision"] + 1 if failure == "wrong_bound_revision"
                else binding["request_revision"]
            ),
        )

    with pytest.raises(RuntimeError, match="stale|binding changed"):
        gr.reconcile_graph(
            gid, _revised_graph(gr, gid),
            repository_names=runtime.repos().keys(),
            expected_revision=current_revision,
            on_reconciled=commit_binding,
        )
    assert _snapshot(runtime, gid) == before
    assert requests.graph_binding(gid) == binding


def test_reconciliation_commits_graph_and_binding_atomically(tmp_path):
    runtime, requests, gr, _eval, gid = setup_graph(tmp_path)
    binding, req = _binding_data(requests, gid)
    updated = _revision_only_reassessment(requests, gid)
    current_revision = gr.get_graph(gid)["revision"]

    def commit_binding(conn):
        requests.bind_graph(
            gid, binding["request_id"], updated["revision"],
            ["backend", "frontend"], binding["requirement_map"],
            replace=True, connection=conn,
            expected_bound_revision=binding["request_revision"],
        )

    result = gr.reconcile_graph(
        gid, _revised_graph(gr, gid),
        repository_names=runtime.repos().keys(),
        expected_revision=current_revision,
        on_reconciled=commit_binding,
    )
    assert result["revision"] == current_revision + 1
    assert "backend" in result["reconciliation"]["changed_nodes"]
    assert requests.graph_binding(gid)["request_revision"] == updated["revision"]
    assert result["nodes"][0]["semantic_state"] == "pending"
    with sqlite3.connect(runtime.db) as db:
        snapshot = db.execute(
            "SELECT request_revision FROM task_graph_binding_snapshots "
            "WHERE graph_run_id=?", (gid,),
        ).fetchone()
    assert snapshot == (updated["revision"],)
