"""Lead-facing MCP orchestration without replacing Discovery or semantic review."""
import asyncio
import importlib
import subprocess

import pytest
from pydantic import ValidationError

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


def test_source_schema_rejects_fields_invalid_for_kind():
    from qiqi_delegate.server import WorkspaceFileSource
    with pytest.raises(ValidationError):
        WorkspaceFileSource(kind="workspace_file", path="spec.md", label="invalid")


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
    compact = asyncio.run(server.get_task_request(rid))
    assert len(str(compact)) < 5000
    assert "content" not in compact["sources"][0]
    full = asyncio.run(server.get_task_request(rid, include_content=True))
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
    text = "Report: " + "A" * 20000
    _, delegate = settled_delegate(runtime, text)
    monkeypatch.setattr(runtime, "delegate", delegate)
    submitted = asyncio.run(server.submit_workflow(
        "Analyze flow", assessment(server), single_task=node(server),
    ))
    gid = submitted["graph_run_id"]
    review = submitted["review_bundle"]["reviews"][0]
    assert review["result"]["response_truncated"]
    assert len(review["result"]["agent_response"]) == 3000
    chunk = asyncio.run(server.get_review_evidence(
        gid, "A", review["attempt_id"], 3000, 8000,
        submitted["review_bundle"]["revision"],
    ))
    assert chunk["agent_response"] == text[3000:11000]
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
        assert db.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == 0


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
