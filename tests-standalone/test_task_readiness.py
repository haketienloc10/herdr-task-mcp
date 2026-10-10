"""Issue #6: task readiness is optional-source, auditable and dispatch-gated."""
import asyncio
import subprocess
from pathlib import Path

import pytest

from qiqi_delegate.core import build_task_packet, render_task_prompt
from qiqi_delegate.runtime import DelegateRuntime
from qiqi_delegate.task_graph_runtime import GraphRuntime, task_graph_from_payload
from qiqi_delegate.task_graph_store import GraphRuntimeStore
from qiqi_delegate.task_request import TaskRequestStore


def workspace(tmp_path: Path):
    control = tmp_path / "control"
    control.mkdir()
    for name in ("backend", "frontend"):
        path = tmp_path / name
        path.mkdir()
        subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (control / "repos.yaml").write_text(
        "repositories:\n  - name: backend\n    path: ../backend\n"
        "  - name: frontend\n    path: ../frontend\n",
        encoding="utf-8",
    )
    (control / "agent-routing.yaml").write_text(
        'routes:\n  codex-balanced:\n    agent: codex\n    args: ["--yolo"]\n',
        encoding="utf-8",
    )
    runtime = DelegateRuntime(control)
    store = TaskRequestStore(runtime.db, control, runtime.repos)
    return runtime, store


def assessment(*, decision="direct", unknowns=None, source="request:current"):
    return {
        "requirements": [
            {"id": "R1", "text": "Retry 429/503 and keep public API",
             "evidence_refs": [source]}
        ],
        "blocking_unknowns": unknowns or [],
        "decision": decision,
        "rationale": "User request already specifies action" if decision == "direct"
                     else "Missing runtime evidence",
    }


def test_direct_without_documents_reuses_context_after_restart(tmp_path):
    runtime, store = workspace(tmp_path)
    original = "Retry 429 and 503 in backend; keep API; add tests"
    request = store.create(original)
    assert request["sources"] == []
    assert request["revision"] == 1
    current = store.assess(request["request_id"], 1, assessment())
    assert current["revision"] == 2
    assert store.assert_ready(request["request_id"], 2)["user_request"] == original

    resumed = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    assert resumed.assert_ready(request["request_id"])["assessment"]["decision"] == "direct"
    with pytest.raises(RuntimeError, match="stale"):
        resumed.assess(request["request_id"], 1, assessment())


def test_blocked_unknowns_are_not_direct_and_invalid_refs_rejected(tmp_path):
    _, store = workspace(tmp_path)
    current = store.create("Duplicated order creation")
    rid = current["request_id"]
    with pytest.raises(ValueError, match="invalid requirement evidence"):
        store.assess(rid, 1, assessment(source="source:nonexistent"))
    with pytest.raises(ValueError, match="blocking unknowns"):
        store.assess(rid, 1, assessment(unknowns=["What causes duplicate orders?"]))
    discovery = store.assess(
        rid, 1, assessment(decision="full_discovery",
                           unknowns=["Which service creates duplicates?"]),
    )
    assert discovery["assessment"]["decision"] == "full_discovery"
    with pytest.raises(ValueError, match="blocked"):
        store.assert_ready(rid)


def test_optional_spec_content_and_freshness(tmp_path):
    runtime, store = workspace(tmp_path)
    src = tmp_path / "backend" / "spec.md"
    src.write_text("Do not change public API", encoding="utf-8")
    req = store.create("Implement according to spec", sources=[
        {"kind": "repo_file", "repository": "backend", "path": "spec.md"}
    ])
    sid = req["sources"][0]["id"]
    assert req["sources"][0]["content"] == "Do not change public API"
    ready = store.assess(req["request_id"], 1, assessment(source=sid))
    assert ready["stale_sources"] == []
    store.assert_ready(req["request_id"], ready["revision"])
    src.write_text("Requirements changed!", encoding="utf-8")
    assert store.get(req["request_id"])["stale_sources"] == [sid]
    with pytest.raises(ValueError, match="stale"):
        store.assert_ready(req["request_id"])
    with pytest.raises(RuntimeError, match="stale"):
        store.assess(req["request_id"], 1, assessment())


@pytest.mark.parametrize("payload", [
    {"kind": "repo_file", "repository": "backend", "path": "../frontend/spec.md"},
    {"kind": "repo_file", "repository": "not-registered", "path": "spec.md"},
    {"kind": "workspace_file", "path": "../../spec.md"},
    {"kind": "inline", "text": ""},
])
def test_invalid_sources_fail_closed(tmp_path, payload):
    _, store = workspace(tmp_path)
    with pytest.raises(ValueError):
        store.create("Investigate", sources=[payload])


def test_symlink_source_escape_fails(tmp_path):
    _, store = workspace(tmp_path)
    path = tmp_path / "backend" / "linked.md"
    path.symlink_to(tmp_path / "frontend" / "external.md")
    with pytest.raises(ValueError, match="symlink"):
        store.create("Investigate", sources=[
            {"kind": "repo_file", "repository": "backend", "path": "linked.md"}
        ])


def test_append_peer_response_invalidates_assessment(tmp_path):
    runtime, store = workspace(tmp_path)
    req = store.create("Understand the code")
    store.assess(req["request_id"], 1, assessment())
    with runtime._connect() as db:
        db.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                   ("turn-123", "native-123", "backend", "codex-balanced",
                    "settled", "Observed contract in backend/api.py:12", 123))
    updated = store.append(req["request_id"], 2,
                           {"kind": "peer_turn", "turn_id": "turn-123"})
    assert updated["revision"] == 3
    assert updated["assessment"] is None
    assert updated["sources"][0]["verification"] == "peer_observed"
    with pytest.raises(ValueError, match="blocked"):
        store.assert_ready(req["request_id"])
    store.assess(req["request_id"], 3, assessment(source=updated["sources"][0]["id"]))


def test_bound_graph_guards_dispatch_and_maps_requirements(tmp_path):
    runtime, store = workspace(tmp_path)
    req = store.create("Implement retry on backend")
    ready = store.assess(req["request_id"], 1, assessment())
    graph = task_graph_from_payload({"nodes": [{
        "node_id": "implement", "repository": "backend", "route": "codex-balanced",
        "task_packet": {
            "objective": "Implement retry", "scope": ["src"],
            "acceptance_criteria": ["Retry 429 and 503"]
        }
    }]})
    gr = GraphRuntime(GraphRuntimeStore(runtime.db),
                      readiness_guard=store.assert_graph_ready)
    started = gr.start_graph(graph, repository_names=runtime.repos().keys())
    gid = started["graph_run_id"]
    with pytest.raises(ValueError, match="invalid graph requirement refs"):
        store.bind_graph(gid, req["request_id"], 2, ["implement"],
                         {"implement": ["R-not-present"]})
    store.bind_graph(gid, req["request_id"], ready["revision"],
                     ["implement"], {"implement": ["R1"]})
    assert store.graph_binding(gid)["requirement_map"] == {"implement": ["R1"]}

    async def execute(node):
        return {"state": "settled", "session_id": "session", "turn_id": "turn",
                "agent_response": "Implemented"}

    result = asyncio.run(gr.delegate_next(gid, executor=execute))
    assert result["graph_state"] == "awaiting_review"
    # A later revision invalidates the original binding instead of silently running stale tasks.
    store.append(req["request_id"], 2, {"kind": "inline", "text": "New constraint"})
    with pytest.raises(RuntimeError, match="stale"):
        store.assert_graph_ready(gid)


def test_discovery_prompt_is_no_write_and_not_single_repo_bound():
    packet = build_task_packet(
        objective="Investigate duplicate requests",
        scope=["backend", "frontend"],
        acceptance_criteria=["Report path:line"],
    )
    prompt = render_task_prompt(
        packet, discovery_repositories=("backend", "frontend"),
    )
    assert "Do NOT create, modify, delete or rename any file" in prompt
    assert "backend, frontend" in prompt
    assert "Operate only inside the current Git root" not in prompt
    normal = render_task_prompt(packet)
    assert "Operate only inside the current Git root" in normal


def test_discovery_keeps_yolo_and_constructs_add_dir_from_registry(tmp_path, monkeypatch):
    runtime, _ = workspace(tmp_path)
    import qiqi_delegate.runtime as mod
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/bin/herdr")
    capture = {}
    async def ensure():
        return None
    async def cmd(*args, **kwargs):
        if args[:2] == ("workspace", "close"):
            return 0, "", ""
        raise AssertionError(args)
    async def rpc(*args, **kwargs):
        assert args[0:2] == ("workspace", "create")
        return {"workspace": {"workspace_id": "wid"},
                "root_pane": {"pane_id": "pid"}}
    async def start(pane, adapter, args):
        capture["args"] = args
        return "qiqi-test", {
            "agent_session": {"kind": "id", "agent": "codex", "value": "native-1"}
        }
    async def prompt(name, text, adapter):
        capture["prompt"] = text
        return "settled", {
            "agent_session": {"kind": "id", "agent": "codex", "value": "native-1"}
        }
    async def captured(*args):
        return {"state": "settled", "agent_response": "Inspected both repositories"}
    monkeypatch.setattr(runtime, "_ensure_herdr_server", ensure)
    monkeypatch.setattr(runtime, "_run", cmd)
    monkeypatch.setattr(runtime, "_json", rpc)
    monkeypatch.setattr(runtime, "_start_agent", start)
    monkeypatch.setattr(runtime, "_prompt", prompt)
    monkeypatch.setattr(runtime, "_capture", captured)

    packet = build_task_packet(
        objective="Discover flow", scope=["backend", "frontend"],
        acceptance_criteria=["File:line evidence"],
    )
    result = asyncio.run(runtime.delegate(
        repository="backend", route="codex-balanced", packet=packet,
        discovery_repositories=("backend", "frontend"),
    ))
    assert result["state"] == "settled"
    args = capture["args"]
    assert "--yolo" in args
    assert "--add-dir" in args
    assert str(tmp_path / "frontend") in args
    assert "Do NOT create, modify, delete" in capture["prompt"]

    with pytest.raises(ValueError, match="unregistered"):
        asyncio.run(runtime.delegate(
            repository="backend", route="codex-balanced", packet=packet,
            discovery_repositories=("backend", "unregistered"),
        ))



def test_discovery_audit_survives_restart(tmp_path):
    runtime, store = workspace(tmp_path)
    request = store.create("Investigate the duplicate order bug")
    assessed = store.assess(
        request["request_id"], 1,
        assessment(decision="targeted_discovery",
                   unknowns=["Is idempotency already implemented?"]),
    )
    ident = store.begin_discovery(
        request["request_id"], "targeted_discovery",
        ["backend", "frontend"], ["Inspect idempotency"], "codex-balanced",
    )
    store.finish_discovery(ident, "settled", turn_id="captured-turn")
    resumed = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    item = resumed.get(request["request_id"])
    assert item["revision"] == assessed["revision"]
    assert item["discoveries"][0]["questions"] == ["Inspect idempotency"]
    assert item["discoveries"][0]["turn_id"] == "captured-turn"


def test_accepted_graph_node_is_a_traced_source(tmp_path):
    runtime, store = workspace(tmp_path)
    graph = task_graph_from_payload({"nodes": [{
        "node_id": "discover", "repository": "backend", "route": "codex-balanced",
        "task_packet": {
            "objective": "Inspect contract", "scope": ["src"],
            "acceptance_criteria": ["File:line evidence"]
        }
    }]})
    gr = GraphRuntime(GraphRuntimeStore(runtime.db))
    started = gr.start_graph(graph, repository_names=runtime.repos().keys())
    gid = started["graph_run_id"]

    async def execute(node):
        return {"state": "settled", "session_id": "native",
                "turn_id": "turn-accepted",
                "agent_response": "Observed contract at backend/api.py:24"}
    completed = asyncio.run(gr.delegate_next(gid, executor=execute))
    from qiqi_delegate.task_graph_runtime import decisions_from_payload
    gr.submit_decisions(
        gid, decisions_from_payload([{"node_id": "discover", "action": "accept"}]),
        expected_revision=completed["revision"],
        lead_dispositions=[{
            "turn_id": "turn-accepted", "action": "accept",
            "reason": "Evidence reviewed", "node_id": "discover",
        }],
    )
    req = store.create("Implement accepted contract", sources=[
        {"kind": "accepted_graph_node", "graph_run_id": gid, "node_id": "discover"}
    ])
    src = req["sources"][0]
    assert src["verification"] == "accepted_peer_evidence"
    assert "backend/api.py:24" in src["content"]


def test_graph_peer_prompt_carries_request_and_accepted_upstream(tmp_path, monkeypatch):
    runtime, store = workspace(tmp_path)
    request = store.create("Inspect contract then implement API client")
    assessment_payload = {
        "requirements": [
            {"id": "R1", "text": "Inspect API contract", "evidence_refs": ["request:current"]},
            {"id": "R2", "text": "Implement client", "evidence_refs": ["request:current"]},
        ],
        "blocking_unknowns": [], "decision": "direct",
        "rationale": "Two actionable tasks with explicit ordering",
    }
    current = store.assess(request["request_id"], 1, assessment_payload)
    graph = task_graph_from_payload({"nodes": [
        {
            "node_id": "A", "repository": "backend", "route": "codex-balanced",
            "task_packet": {"objective": "Inspect contract", "scope": ["src"],
                            "acceptance_criteria": ["Capture contract with file:line"]}
        },
        {
            "node_id": "B", "repository": "frontend", "route": "codex-balanced",
            "depends_on": ["A"],
            "task_packet": {"objective": "Implement client", "scope": ["src"],
                            "acceptance_criteria": ["Pass client tests"]}
        }
    ]})
    gr = GraphRuntime(GraphRuntimeStore(runtime.db),
                      readiness_guard=store.assert_graph_ready)
    gid = gr.start_graph(graph, repository_names=runtime.repos().keys())["graph_run_id"]
    store.bind_graph(gid, request["request_id"], current["revision"],
                     ["A", "B"], {"A": ["R1"], "B": ["R2"]})
    async def execute(node):
        return {"state": "settled", "session_id": "native-" + node.node_id,
                "turn_id": "turn-" + node.node_id,
                "agent_response": "POST /orders response from backend/api.py:24"}
    executed = asyncio.run(gr.delegate_next(gid, executor=execute))
    from qiqi_delegate.task_graph_runtime import decisions_from_payload
    gr.submit_decisions(
        gid, decisions_from_payload([{"node_id": "A", "action": "accept"}]),
        expected_revision=executed["revision"],
        lead_dispositions=[{
            "turn_id": "turn-A", "action": "accept", "reason": "Reviewed", "node_id": "A",
        }],
    )

    monkeypatch.setenv("QIQI_WORKSPACE_ROOT", str(runtime.root))
    import importlib
    server = importlib.import_module("qiqi_delegate.server")
    monkeypatch.setattr(server, "task_requests", store)
    monkeypatch.setattr(server, "graph_runtime", gr)
    captured = {}
    async def fake_delegate(**kwargs):
        captured["packet"] = kwargs["packet"]
        return {"state": "settled"}
    monkeypatch.setattr(server.runtime, "delegate", fake_delegate)
    node = next(n for n in graph.nodes if n.node_id == "B")
    asyncio.run(server._graph_execute(gid, node))
    prompt = render_task_prompt(captured["packet"])
    assert "Original user request (verbatim): Inspect contract then implement API client" in prompt
    assert "Requirements for this node: Implement client" in prompt
    assert "Accepted upstream Peer report (A;" in prompt
    assert "backend/api.py:24" in prompt
