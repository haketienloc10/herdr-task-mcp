"""Lead-facing MCP orchestration without replacing Discovery or semantic review."""
import asyncio
import importlib
import json
import subprocess

import pytest
from pydantic import ValidationError
from mcp.server.mcpserver.exceptions import ToolError

from qiqi_delegate.runtime import DelegateRuntime
from qiqi_delegate.task_graph_runtime import GraphRuntime, task_graph_from_payload
from qiqi_delegate.task_graph_store import GraphRuntimeStore
from qiqi_delegate.task_request import TaskRequestStore


def environment(tmp_path, monkeypatch):
    control = tmp_path / "control"
    control.mkdir()
    repo = tmp_path / "backend"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (control / "repos.yaml").write_text(
        "repositories:\n  - name: backend\n    path: ../backend\n",
        encoding="utf-8",
    )
    (control / "agent-routing.yaml").write_text(
        'routes:\n  codex-balanced:\n    agent: codex\n    args: []\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("QIQI_WORKSPACE_ROOT", str(control))
    runtime = DelegateRuntime(control)
    store = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    graph_runtime = GraphRuntime(
        GraphRuntimeStore(runtime.db),
        repository_key=lambda name: str(runtime.repos()[name]),
        readiness_guard=store.assert_graph_ready,
    )
    server = importlib.import_module("qiqi_delegate.server")
    monkeypatch.setattr(server, "runtime", runtime)
    monkeypatch.setattr(server, "task_requests", store)
    monkeypatch.setattr(server, "graph_runtime", graph_runtime)
    return server, runtime, store, graph_runtime


def assessment(server, refs=("request:current",), decision="direct", unknowns=None):
    return server.ReadinessAssessment(
        requirements=[
            server.RequirementInput(
                id="R1", text="Trace behavior or implement a narrow change",
                evidence_refs=list(refs),
            )
        ],
        blocking_unknowns=unknowns or [], decision=decision,
        rationale="Evidence-linked readiness",
    )


def node(server, node_id="A"):
    return server.Node(
        node_id=node_id, repository="backend", route="codex-balanced",
        task_packet=server.Packet(
            objective=f"Investigate and report {node_id}",
            scope=["backend"], acceptance_criteria=["Verified report"],
        ),
    )


def settled_delegate(runtime, response="backend/flow.py:20 verified"):
    calls = []

    async def delegate(**kwargs):
        turn_id = f"turn-{len(calls) + 1}"
        calls.append(kwargs)
        with runtime._connect() as db:
            db.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)", (
                turn_id, f"session-{len(calls)}", "backend", "codex-balanced",
                "settled", response, len(calls),
            ))
        return {
            "state": "settled", "turn_id": turn_id,
            "session_id": f"session-{len(calls)}", "agent_response": response,
        }

    return calls, delegate


def test_source_schema_rejects_fields_invalid_for_kind(tmp_path, monkeypatch):
    server, _, _, _ = environment(tmp_path, monkeypatch)
    with pytest.raises(ValidationError):
        server.WorkspaceFileSource(kind="workspace_file", path="spec.md", label="invalid")


def test_create_assessed_is_atomic_and_resolves_aliases(tmp_path, monkeypatch):
    server, _, store, _ = environment(tmp_path, monkeypatch)
    request = store.create_assessed(
        "Trace request", [{"kind": "inline", "text": "Expected behavior"}],
        assessment(server, refs=("alias:spec",)).model_dump(),
        source_aliases=["spec"],
    )
    assert request["revision"] == 2
    assert "content" not in request["sources"][0]
    assert store.get(request["request_id"])["sources"][0]["content"] == "Expected behavior"
    assert store.assert_ready(request["request_id"])["assessment"][
        "requirements"][0]["evidence_refs"] == [request["sources"][0]["id"]]
    with pytest.raises(ValueError, match="invalid requirement evidence"):
        store.create_assessed(
            "Another request", [{"kind": "inline", "text": "Evidence"}],
            assessment(server, refs=("alias:missing",)).model_dump(),
            source_aliases=["spec"],
        )
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM task_requests").fetchone()[0] == 1


def test_submit_workflow_compact_sources_and_exact_pagination(tmp_path, monkeypatch):
    server, _, store, _ = environment(tmp_path, monkeypatch)
    content = "X" * 75000
    result = asyncio.run(server.submit_workflow(
        "Investigate backend",
        assessment(server, refs=("request:current", "alias:spec")),
        sources=[server.WorkflowSource(
            alias="spec", source=server.InlineSource(kind="inline", text=content),
        )],
        single_task=node(server), auto_dispatch=False,
    ))
    assert result["state"] == "ready"
    assert "content" not in result["task_request"]["sources"][0]
    rid = result["task_request"]["request_id"]
    sid = result["source_aliases"]["spec"]
    compact = asyncio.run(server.get_task_request_summary(rid))
    assert len(str(compact)) < 5000
    assert "content" not in compact["sources"][0]
    assert "content" not in asyncio.run(
        server.get_task_request(rid, include_content=False)
    )["sources"][0]
    full = asyncio.run(server.get_task_request(rid))
    assert full["sources"][0]["content"] == content
    first = asyncio.run(server.get_task_source(rid, sid, 0, 12000))
    assert first["next_offset"] == 12000
    assert first["content"] == content[:12000]
    second = asyncio.run(server.get_task_source(rid, sid, 12000, 12000))
    assert second["content"] == content[12000:24000]
    assert store.graph_binding(result["graph_run_id"])["request_revision"] == 2


def test_auto_dispatch_and_auto_continue_keep_explicit_lead_accept(tmp_path, monkeypatch):
    server, runtime, _, gr = environment(tmp_path, monkeypatch)
    calls, delegate = settled_delegate(runtime, "backend/flow.py:20 verified")
    monkeypatch.setattr(runtime, "delegate", delegate)
    criteria = server.ReadinessAssessment(
        requirements=[
            server.RequirementInput(id="R1", text="Analyze A",
                                    evidence_refs=["request:current"]),
            server.RequirementInput(id="R2", text="Analyze B",
                                    evidence_refs=["request:current"]),
        ],
        blocking_unknowns=[], decision="direct", rationale="Two independent analyses",
    )
    submitted = asyncio.run(server.submit_workflow(
        "Investigate two independent questions", criteria,
        graph=server.Graph(nodes=[node(server, "A"), node(server, "B")]),
        requirement_map={"A": ["R1"], "B": ["R2"]},
    ))
    assert len(calls) == 1  # same repository: wave is still conflict-free
    assert submitted["review_bundle"]["graph_state"] == "awaiting_review"
    assert submitted["review_bundle"]["reviews"][0]["node_id"] == "A"
    gid = submitted["graph_run_id"]
    first_revision = submitted["review_bundle"]["revision"]
    # Explicit ACCEPT remains a mandatory semantic transition.
    continued = asyncio.run(server.submit_decisions(
        gid, [server.Decision(node_id="A", action="accept")],
        first_revision, auto_continue=True,
    ))
    assert continued["continuation_error"] is None
    assert len(calls) == 2
    assert continued["review_bundle"]["reviews"][0]["node_id"] == "B"
    assert gr.get_graph(gid)["graph_state"] == "awaiting_review"
    completed = asyncio.run(server.submit_decisions(
        gid, [server.Decision(node_id="B", action="accept")],
        continued["review_bundle"]["revision"], auto_continue=True,
    ))
    assert completed["review_bundle"]["graph_state"] == "complete"
    assert len(calls) == 2


def test_review_bundle_truncation_and_exact_chunk_validation(tmp_path, monkeypatch):
    server, runtime, _, _ = environment(tmp_path, monkeypatch)
    text = "Report: " + "A" * 100000
    _, delegate = settled_delegate(runtime, text)
    monkeypatch.setattr(runtime, "delegate", delegate)
    submitted = asyncio.run(server.submit_workflow(
        "Analyze flow", assessment(server), single_task=node(server),
    ))
    gid = submitted["graph_run_id"]
    bundle = submitted["review_bundle"]
    review = bundle["reviews"][0]
    assert review["result"]["response_truncated"]
    preview = review["result"]["agent_response"]
    assert 10000 < len(preview) < 32000
    assert review["result"]["next_offset"] == len(preview)
    assert review["result"]["total_response_chars"] == len(text)
    assert len(json.dumps(bundle, ensure_ascii=True)) <= 32000
    chunk = asyncio.run(server.get_review_evidence(
        gid, "A", review["attempt_id"], len(preview), 8000,
        bundle["revision"],
    ))
    assert chunk["agent_response"] == text[len(preview):len(preview) + 8000]
    with pytest.raises(Exception, match="stale review attempt"):
        asyncio.run(server.get_review_evidence(gid, "A", "wrong-attempt"))


def test_discovery_preserved_and_requires_reassessment(tmp_path, monkeypatch):
    server, runtime, store, _ = environment(tmp_path, monkeypatch)
    calls, delegate = settled_delegate(runtime, "backend/flow.py:20 unknown")
    monkeypatch.setattr(runtime, "delegate", delegate)
    output = asyncio.run(server.submit_workflow(
        "Find cause of intermittent failure",
        assessment(server, decision="targeted_discovery",
                   unknowns=["Where does retry originate?"]),
        discovery=server.DiscoveryPlan(
            repository_names=["backend"], route="codex-balanced",
            questions=["Trace retry origin with file:line"],
            mode="targeted_discovery",
        ),
    ))
    assert len(calls) == 1
    assert calls[0]["discovery_repositories"] == ("backend",)
    assert output["state"] == "discovery_complete"
    assert output["captured_source_id"]
    assert output["task_request"]["assessment"] is None
    assert len(output["task_request"]["sources"]) == 1
    source = asyncio.run(server.get_task_source(
        output["task_request"]["request_id"], output["captured_source_id"],
    ))
    assert "backend/flow.py:20" in source["content"]
    with pytest.raises(ValueError, match="blocked"):
        store.assert_ready(output["task_request"]["request_id"])
    request_id = output["task_request"]["request_id"]
    continued = asyncio.run(server.continue_workflow(
        request_id, output["task_request"]["revision"],
        assessment(server, refs=("request:current", output["captured_source_id"])),
        single_task=node(server),
    ))
    assert continued["task_request"]["request_id"] == request_id
    assert continued["task_request"]["revision"] == output["task_request"]["revision"] + 1
    assert continued["review_bundle"]["graph_state"] == "awaiting_review"
    assert len(calls) == 2
    assert output["result_state"] == "settled"
    assert store.graph_binding(continued["graph_run_id"])["request_id"] == request_id
    assert "backend/flow.py:20" in str(calls[1]["packet"].as_dict())


def test_oversized_initial_context_refused_before_graph_dispatch(tmp_path, monkeypatch):
    server, runtime, store, _ = environment(tmp_path, monkeypatch)
    calls, delegate = settled_delegate(runtime)
    monkeypatch.setattr(runtime, "delegate", delegate)
    with pytest.raises(Exception, match="TaskPacket exceeds the size limit"):
        asyncio.run(server.submit_workflow(
            "Investigate", assessment(server, refs=("alias:large",)),
            sources=[server.WorkflowSource(
                alias="large",
                source=server.InlineSource(kind="inline", text="Z" * 99900),
            )],
            single_task=node(server),
        ))
    assert not calls
    with store._connect() as db:
        table = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_runs'"
        ).fetchone()
        assert table is None or db.execute(
            "SELECT COUNT(*) FROM graph_runs"
        ).fetchone()[0] == 0


def test_independent_wave_runs_while_other_node_awaits_review(tmp_path, monkeypatch):
    server, _, _, gr = environment(tmp_path, monkeypatch)
    graph = task_graph_from_payload(server.Graph(
        nodes=[node(server, "A"), node(server, "B")],
    ).model_dump())
    started = gr.start_graph(graph, repository_names=["backend"])
    async def executor(item):
        return {
            "state": "settled", "turn_id": f"turn-{item.node_id}",
            "session_id": f"session-{item.node_id}",
            "agent_response": f"backend/{item.node_id}.py:10 verified",
        }
    first = asyncio.run(gr.delegate_next(started["graph_run_id"], executor=executor))
    assert first["graph_state"] == "awaiting_review"
    assert first["runnable_nodes"] == ["B"]
    second = asyncio.run(gr.delegate_next(started["graph_run_id"], executor=executor))
    assert {r["node_id"] for r in second["review_required"]} == {"A", "B"}


def test_review_bundle_pagination_covers_more_than_first_page(tmp_path, monkeypatch):
    server, _, _, gr = environment(tmp_path, monkeypatch)
    graph = task_graph_from_payload(server.Graph(
        nodes=[node(server, str(i)) for i in range(6)],
    ).model_dump())
    gid = gr.start_graph(graph, repository_names=["backend"])["graph_run_id"]

    async def executor(item):
        return {
            "state": "settled", "turn_id": "turn-" + item.node_id,
            "session_id": "session-" + item.node_id,
            "agent_response": "report for " + item.node_id,
        }

    for _ in range(6):
        asyncio.run(gr.delegate_next(gid, executor=executor))
    first = asyncio.run(server.get_review_bundle(gid, max_reviews=4))
    assert first["review_required_count"] == 6
    assert first["more_reviews"]
    assert first["next_review_offset"] == 4
    with pytest.raises(ToolError, match="expected_revision is required"):
        asyncio.run(server.get_review_bundle(gid, review_offset=4))
    second = asyncio.run(server.get_review_bundle(
        gid, review_offset=4, expected_revision=first["revision"],
    ))
    assert [item["node_id"] for item in second["reviews"]] == ["4", "5"]
    assert second["next_review_offset"] is None


def test_failed_first_dispatch_returns_durable_graph_identity(tmp_path, monkeypatch):
    server, runtime, _, gr = environment(tmp_path, monkeypatch)

    async def failed_delegate(**kwargs):
        raise RuntimeError("simulated transport failure")

    monkeypatch.setattr(runtime, "delegate", failed_delegate)
    output = asyncio.run(server.submit_workflow(
        "Analyze the backend", assessment(server), single_task=node(server),
    ))
    assert output["state"] == "dispatch_failed"
    assert "simulated transport failure" in output["dispatch_error"]
    assert output["graph_run_id"]
    persisted = gr.get_graph(output["graph_run_id"])
    assert persisted["review_required"][0]["node_id"] == "A"
    assert persisted["review_required"][0]["runtime_state"] == "failed"


def test_review_pages_fail_closed_after_intervening_lead_accept(tmp_path, monkeypatch):
    server, _, _, gr = environment(tmp_path, monkeypatch)
    graph = task_graph_from_payload(server.Graph(
        nodes=[node(server, str(i)) for i in range(6)],
    ).model_dump())
    gid = gr.start_graph(graph, repository_names=["backend"])["graph_run_id"]

    async def executor(item):
        return {
            "state": "settled", "turn_id": "turn-" + item.node_id,
            "session_id": "session-" + item.node_id,
            "agent_response": "validated report: " + item.node_id,
        }

    for _ in range(6):
        asyncio.run(gr.delegate_next(gid, executor=executor))
    page1 = asyncio.run(server.get_review_bundle(gid))
    assert [r["node_id"] for r in page1["reviews"]] == ["0", "1", "2", "3"]
    from qiqi_delegate.task_graph_runtime import decisions_from_payload
    gr.submit_decisions(
        gid, decisions_from_payload([{"node_id": "0", "action": "accept"}]),
        expected_revision=page1["revision"],
    )
    # The review set has shifted. Never silently return only item 5 and lose 4.
    with pytest.raises(ToolError, match="expected_revision is required"):
        asyncio.run(server.get_review_bundle(gid, review_offset=4))
    with pytest.raises(ToolError, match="stale graph snapshot"):
        asyncio.run(server.get_review_bundle(
            gid, review_offset=4, expected_revision=page1["revision"],
        ))


def test_replan_is_graph_wide_fail_closed_even_while_other_reviews_pending(
    tmp_path, monkeypatch,
):
    server, runtime, _, gr = environment(tmp_path, monkeypatch)
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    subprocess.run(["git", "-C", str(frontend), "init", "-q"], check=True)
    (runtime.root / "repos.yaml").write_text(
        "repositories:\n  - name: backend\n    path: ../backend\n"
        "  - name: frontend\n    path: ../frontend\n",
        encoding="utf-8",
    )
    frontend_node = node(server, "B").model_copy(update={"repository": "frontend"})
    authored = task_graph_from_payload(server.Graph(
        nodes=[node(server, "A"), frontend_node, node(server, "C")],
    ).model_dump())
    gid = gr.start_graph(
        authored, repository_names=runtime.repos().keys(),
    )["graph_run_id"]
    dispatched = []

    async def executor(item):
        dispatched.append(item.node_id)
        return {
            "state": "settled", "turn_id": "turn-" + item.node_id,
            "session_id": "session-" + item.node_id,
            "agent_response": "verified " + item.node_id,
        }

    first = asyncio.run(gr.delegate_next(gid, executor=executor))
    assert dispatched == ["A", "B"]
    # A's replan cannot permit C's unrelated pending work to race ahead,
    # even though B still needs review and aggregate state awaits review.
    response = asyncio.run(server.submit_decisions(
        gid,
        [server.Decision(
            node_id="A", action="replan", owner="Lead",
            return_checkpoint="reconcile graph",
        )],
        first["revision"], auto_continue=True,
    ))
    assert response["review_bundle"]["graph_state"] == "awaiting_review"
    assert response["review_bundle"]["runnable_nodes"] == ["C"]
    assert response["continuation_results"] == []
    assert response["continuation_error"] is None
    assert dispatched == ["A", "B"]
    with pytest.raises(RuntimeError, match="blocked/replanned nodes"):
        asyncio.run(gr.delegate_next(gid, executor=executor))
    assert dispatched == ["A", "B"]


def test_legacy_source_null_fields_and_full_result_contract(tmp_path, monkeypatch):
    server, _, store, _ = environment(tmp_path, monkeypatch)
    (tmp_path / "backend" / "spec.md").write_text("Legacy requirements")
    variants = [
        ("inline", {"text": "note"}),
        ("repo_file", {"repository": "backend", "path": "spec.md"}),
        ("workspace_file", {"path": "notes.md"}),
        ("peer_turn", {"turn_id": "old-turn"}),
        ("accepted_graph_node", {"graph_run_id": "graph", "node_id": "A"}),
    ]
    all_optional = {
        "text": None, "label": None, "repository": None, "path": None,
        "turn_id": None, "graph_run_id": None, "node_id": None,
    }
    for kind, fields in variants:
        transport = server.LegacyRequestSource.model_validate({
            "kind": kind, **all_optional, **fields,
        })
        assert transport.model_dump(exclude_none=True) == {
            "kind": kind, **fields,
        }
    legacy = server.LegacyRequestSource.model_validate({
        "kind": "repo_file", **all_optional,
        "repository": "backend", "path": "spec.md",
    })
    created = asyncio.run(server.prepare_task_request("Inspect spec", [legacy]))
    rid = created["request_id"]
    assert created["sources"][0]["content"] == "Legacy requirements"
    assert asyncio.run(server.get_task_request(rid))["sources"][0][
        "content"
    ] == "Legacy requirements"
    compact = asyncio.run(server.get_task_request_summary(rid))
    assert "content" not in compact["sources"][0]
    appended = asyncio.run(server.add_task_source(
        rid, created["revision"],
        server.LegacyRequestSource(
            kind="inline", text="Another requirement", path=None,
        ),
    ))
    assert len(appended["sources"]) == 2
    assert appended["sources"][1]["content"] == "Another requirement"
    assessed = asyncio.run(server.submit_context_assessment(
        rid, appended["revision"],
        assessment(server, refs=(appended["sources"][0]["id"],)),
    ))
    assert assessed["sources"][0]["content"] == "Legacy requirements"
    assert store.get(rid)["revision"] == assessed["revision"]


def test_medium_peer_response_fits_single_review_bundle_call(tmp_path, monkeypatch):
    server, runtime, _, _ = environment(tmp_path, monkeypatch)
    text = "Verified report: " + "A" * 12000
    _, delegate = settled_delegate(runtime, text)
    monkeypatch.setattr(runtime, "delegate", delegate)
    submitted = asyncio.run(server.submit_workflow(
        "Inspect behavior", assessment(server), single_task=node(server),
    ))
    gid = submitted["graph_run_id"]
    first = submitted["review_bundle"]
    assert first["reviews"][0]["result"]["agent_response"] == text
    assert not first["reviews"][0]["result"]["response_truncated"]
    assert first["reviews"][0]["result"]["next_offset"] is None
    assert len(json.dumps(first, ensure_ascii=True)) <= 32000
    # Existing callers specifying 12000 must no longer hit the old 8000 cap.
    explicit = asyncio.run(server.get_review_bundle(
        gid, max_chars_per_review=12000,
    ))
    assert len(explicit["reviews"][0]["result"]["agent_response"]) == 12000
    assert explicit["reviews"][0]["result"]["next_offset"] == 12000
    assert len(json.dumps(explicit, ensure_ascii=True)) <= 32000
    legacy_cap = asyncio.run(server.get_review_bundle(
        gid, max_chars_per_review=3000,
    ))
    assert len(legacy_cap["reviews"][0]["result"]["agent_response"]) == 3000


def test_adaptive_review_budget_returns_all_short_reviews_in_one_page(
    tmp_path, monkeypatch,
):
    server, _, _, gr = environment(tmp_path, monkeypatch)
    authored = task_graph_from_payload(server.Graph(
        nodes=[node(server, str(i)) for i in range(6)],
    ).model_dump())
    gid = gr.start_graph(authored, repository_names=["backend"])["graph_run_id"]
    sizes = [80, 12000, 150, 3000, 1500, 200]

    async def executor(item):
        return {
            "state": "settled", "turn_id": "turn-" + item.node_id,
            "session_id": "session-" + item.node_id,
            "agent_response": item.node_id * sizes[int(item.node_id)],
        }

    for _ in sizes:
        asyncio.run(gr.delegate_next(gid, executor=executor))
    bundle = asyncio.run(server.get_review_bundle(gid))
    assert bundle["review_required_count"] == 6
    assert len(bundle["reviews"]) == 6
    assert bundle["next_review_offset"] is None
    assert len(json.dumps(bundle, ensure_ascii=True)) <= 32000
    for review in bundle["reviews"]:
        result = review["result"]
        expected = review["node_id"] * sizes[int(review["node_id"])]
        assert result["agent_response"] == expected
        assert result["response_truncated"] is False
        assert result["next_offset"] is None


def test_adaptive_budget_fairness_exact_chunks_and_escaped_json(tmp_path, monkeypatch):
    server, _, _, gr = environment(tmp_path, monkeypatch)
    authored = task_graph_from_payload(server.Graph(
        nodes=[node(server, "A"), node(server, "B")],
    ).model_dump())
    gid = gr.start_graph(authored, repository_names=["backend"])["graph_run_id"]
    responses = {"A": '"\\\\\n🎯' * 13000, "B": "B" * 60000}

    async def executor(item):
        return {
            "state": "settled", "turn_id": "turn-" + item.node_id,
            "session_id": "session-" + item.node_id,
            "agent_response": responses[item.node_id],
        }

    asyncio.run(gr.delegate_next(gid, executor=executor))
    asyncio.run(gr.delegate_next(gid, executor=executor))
    bundle = asyncio.run(server.get_review_bundle(
        gid, max_total_chars=20000, max_chars_per_review=12000,
    ))
    assert len(json.dumps(bundle, ensure_ascii=True)) <= 20000
    assert len(bundle["reviews"]) == 2
    lengths = [len(x["result"]["agent_response"]) for x in bundle["reviews"]]
    assert all(0 < length <= 12000 for length in lengths)
    for review in bundle["reviews"]:
        preview = review["result"]["agent_response"]
        source = responses[review["node_id"]]
        assert source.startswith(preview)
        assert review["result"]["total_response_chars"] == len(source)
        assert review["result"]["next_offset"] == len(preview)
        chunk = asyncio.run(server.get_review_evidence(
            gid, review["node_id"], review["attempt_id"],
            offset=len(preview), max_chars=24000,
            expected_revision=bundle["revision"],
        ))
        assert chunk["agent_response"] == source[len(preview):len(preview) + 24000]
    with pytest.raises(ToolError, match="max_total_chars"):
        asyncio.run(server.get_review_bundle(gid, max_total_chars=1000))
    with pytest.raises(ToolError, match="max_chars_per_review"):
        asyncio.run(server.get_review_bundle(gid, max_chars_per_review=64001))


def test_adaptive_budget_pages_metadata_without_dropping_attempts(
    tmp_path, monkeypatch,
):
    server, _, _, gr = environment(tmp_path, monkeypatch)
    authored_nodes = [
        node(server, str(i)).model_copy(update={
            "task_packet": server.Packet(
                objective="Inspect " + str(i), scope=["backend"],
                acceptance_criteria=["criterion-" + "Z" * 5000],
            ),
        })
        for i in range(5)
    ]
    gid = gr.start_graph(
        task_graph_from_payload(server.Graph(nodes=authored_nodes).model_dump()),
        repository_names=["backend"],
    )["graph_run_id"]

    async def executor(item):
        return {
            "state": "settled", "turn_id": "turn-" + item.node_id,
            "session_id": "session-" + item.node_id,
            "agent_response": "proof " + item.node_id,
        }

    for _ in range(5):
        asyncio.run(gr.delegate_next(gid, executor=executor))
    first = asyncio.run(server.get_review_bundle(gid, max_total_chars=14000))
    assert 0 < len(first["reviews"]) < 5
    assert first["more_reviews"]
    assert len(json.dumps(first, ensure_ascii=True)) <= 14000
    next_offset = first["next_review_offset"]
    seen = [r["node_id"] for r in first["reviews"]]
    while next_offset is not None:
        page = asyncio.run(server.get_review_bundle(
            gid, max_total_chars=14000,
            review_offset=next_offset, expected_revision=first["revision"],
        ))
        assert len(json.dumps(page, ensure_ascii=True)) <= 14000
        assert page["reviews"]
        seen.extend(r["node_id"] for r in page["reviews"])
        next_offset = page["next_review_offset"]
    assert seen == [str(i) for i in range(5)]


def test_installer_teaches_aggregate_review_budget_and_exact_pagination():
    from qiqi_delegate.install import RULES
    assert "max_total_chars" in RULES
    assert "32000" in RULES
    assert "max_chars_per_review" in RULES
    assert "get_review_evidence" in RULES
    assert "next_offset" in RULES
    assert "expected_revision" in RULES
