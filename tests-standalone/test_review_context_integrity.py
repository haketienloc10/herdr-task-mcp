"""Codex PR #7 review regression tests: never silently drop required evidence."""
import asyncio
import importlib

import pytest

from test_task_readiness import workspace
from qiqi_delegate.core import build_task_packet, render_task_prompt
from qiqi_delegate.task_graph_runtime import GraphRuntime, task_graph_from_payload, decisions_from_payload
from qiqi_delegate.task_graph_store import GraphRuntimeStore


def _server(tmp_path, monkeypatch):
    runtime, store = workspace(tmp_path)
    monkeypatch.setenv("QIQI_WORKSPACE_ROOT", str(runtime.root))
    server = importlib.import_module("qiqi_delegate.server")
    graph_runtime = GraphRuntime(
        GraphRuntimeStore(runtime.db),
        readiness_guard=store.assert_graph_ready,
    )
    monkeypatch.setattr(server, "runtime", runtime)
    monkeypatch.setattr(server, "task_requests", store)
    monkeypatch.setattr(server, "graph_runtime", graph_runtime)
    seen = {}

    async def fake_delegate(**kwargs):
        seen.update(kwargs)
        return {"state": "settled", "turn_id": "fake", "agent_response": "Done"}

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    return server, runtime, store, graph_runtime, seen


def _request(store, source_content):
    # More than the old [:8] limit; the only cited source is number 10.
    sources = [{"kind": "inline", "label": f"unrelated-{index}",
                "text": f"Unused context #{index}"} for index in range(9)]
    sources.append({"kind": "inline", "label": "required-contract",
                    "text": source_content})
    request = store.create("Implement order flow", sources=sources)
    relevant_id = request["sources"][9]["id"]
    assessed = store.assess(
        request["request_id"], request["revision"],
        {"requirements": [
            {"id": "R1", "text": "Implement captured order contract",
             "evidence_refs": [relevant_id]}
        ], "blocking_unknowns": [], "decision": "direct",
         "rationale": "The provided contract is actionable"},
    )
    return assessed


def test_direct_delegation_transfers_ninth_or_later_complete_source(tmp_path, monkeypatch):
    server, _, store, _, seen = _server(tmp_path, monkeypatch)
    source = "a" * 7000 + " CRITICAL_TAIL_CONTRACT: POST /orders"
    request = _request(store, source)
    asyncio.run(server.delegate_repo_task(
        "backend", "codex-balanced", "Implement orders", ["src"],
        ["Matches the contract"],
        task_request_id=request["request_id"],
        task_request_revision=request["revision"],
        requirement_refs=["R1"],
    ))
    prompt = render_task_prompt(seen["packet"])
    assert source in prompt
    assert request["sources"][9]["id"] in prompt
    assert "Unused context #0" not in prompt


def test_graph_delegation_transfers_ninth_or_later_complete_source(tmp_path, monkeypatch):
    server, runtime, store, gr, seen = _server(tmp_path, monkeypatch)
    source = "b" * 7000 + " CRITICAL_TAIL_FIELD: order_id"
    request = _request(store, source)
    graph = task_graph_from_payload({"nodes": [
        {"node_id": "B", "repository": "backend", "route": "codex-balanced",
         "task_packet": {"objective": "Implement", "scope": ["src"],
                         "acceptance_criteria": ["Contract correct"]}}
    ]})
    started = gr.start_graph(graph, repository_names=runtime.repos().keys())
    store.bind_graph(started["graph_run_id"], request["request_id"],
                     request["revision"], ["B"], {"B": ["R1"]})
    asyncio.run(server._graph_execute(started["graph_run_id"], graph.nodes[0]))
    prompt = render_task_prompt(seen["packet"])
    assert source in prompt
    assert request["sources"][9]["id"] in prompt
    assert "Unused context #0" not in prompt


def test_downstream_receives_full_accepted_upstream_report(tmp_path, monkeypatch):
    server, runtime, store, gr, seen = _server(tmp_path, monkeypatch)
    source = "x" * 7000 + " CRITICAL_TAIL_UPSTREAM: X-Idempotency-Key required"
    request = store.create("Find contract then update client")
    ready = store.assess(request["request_id"], 1, {
        "requirements": [
            {"id": "R1", "text": "Inspect contract",
             "evidence_refs": ["request:current"]},
            {"id": "R2", "text": "Update client",
             "evidence_refs": ["request:current"]},
        ],
        "blocking_unknowns": [], "decision": "direct",
        "rationale": "Plan is specified",
    })
    graph = task_graph_from_payload({"nodes": [
        {"node_id": "A", "repository": "backend", "route": "codex-balanced",
         "task_packet": {"objective": "Inspect API", "scope": ["src"],
                         "acceptance_criteria": ["File:line"]}},
        {"node_id": "B", "repository": "frontend", "route": "codex-balanced",
         "depends_on": ["A"],
         "task_packet": {"objective": "Implement client", "scope": ["src"],
                         "acceptance_criteria": ["Tests pass"]}},
    ]})
    started = gr.start_graph(graph, repository_names=runtime.repos().keys())
    gid = started["graph_run_id"]
    store.bind_graph(gid, request["request_id"], ready["revision"],
                     ["A", "B"], {"A": ["R1"], "B": ["R2"]})

    async def upstream(_node):
        return {"state": "settled", "session_id": "native-a",
                "turn_id": "turn-a", "agent_response": source}

    result = asyncio.run(gr.delegate_next(gid, executor=upstream))
    gr.submit_decisions(
        gid, decisions_from_payload([{"node_id": "A", "action": "accept"}]),
        expected_revision=result["revision"],
        lead_dispositions=[{"turn_id": "turn-a", "action": "accept",
                            "reason": "Evidence verified", "node_id": "A"}],
    )
    asyncio.run(server._graph_execute(gid, graph.nodes[1]))
    assert source in render_task_prompt(seen["packet"])


def test_oversize_referenced_source_rejected_without_dispatch(tmp_path, monkeypatch):
    server, _, store, _, seen = _server(tmp_path, monkeypatch)
    request = _request(store, "Z" * 99_000 + " REQUIRED_AT_END")
    with pytest.raises(ValueError, match="Nothing was truncated"):
        asyncio.run(server.delegate_repo_task(
            "backend", "codex-balanced", "Implement orders", ["src"],
            ["Matches the contract"], constraints=["padding" * 2000],
            task_request_id=request["request_id"],
            task_request_revision=request["revision"], requirement_refs=["R1"],
        ))
    assert "packet" not in seen


def test_oversize_accepted_upstream_rejected_without_dispatch(tmp_path, monkeypatch):
    server, runtime, store, gr, seen = _server(tmp_path, monkeypatch)
    # Use the normal GraphRuntime state machine to ACCEPT an oversized native capture.
    req = store.create("Inspect API then implement client")
    ready = store.assess(req["request_id"], 1, {
        "requirements": [
            {"id": "R1", "text": "Inspect API", "evidence_refs": ["request:current"]},
            {"id": "R2", "text": "Implement client", "evidence_refs": ["request:current"]}
        ], "blocking_unknowns": [], "decision": "direct",
        "rationale": "Known workflow",
    })
    graph = task_graph_from_payload({"nodes": [
        {"node_id": "A", "repository": "backend", "route": "codex-balanced",
         "task_packet": {"objective": "Inspect", "scope": ["src"],
                         "acceptance_criteria": ["file:line"]}},
        {"node_id": "B", "repository": "frontend", "route": "codex-balanced",
         "depends_on": ["A"],
         "task_packet": {"objective": "Implement", "scope": ["src"],
                         "acceptance_criteria": ["Tests"]}},
    ]})
    gid = gr.start_graph(graph, repository_names=runtime.repos().keys())["graph_run_id"]
    store.bind_graph(gid, req["request_id"], ready["revision"],
                     ["A", "B"], {"A": ["R1"], "B": ["R2"]})

    async def upstream(node):
        return {"state": "settled", "session_id": "native",
                "turn_id": "turn-a", "agent_response": "U" * 101_000 + " IMPORTANT_LAST"}

    result = asyncio.run(gr.delegate_next(gid, executor=upstream))
    gr.submit_decisions(
        gid, decisions_from_payload([{"node_id": "A", "action": "accept"}]),
        expected_revision=result["revision"],
        lead_dispositions=[{"turn_id": "turn-a", "action": "accept",
                            "reason": "Complete", "node_id": "A"}],
    )
    with pytest.raises(ValueError, match="Nothing was truncated"):
        asyncio.run(server._graph_execute(gid, graph.nodes[1]))
    assert "packet" not in seen
