import asyncio
import json
import os
import subprocess
import sys

import pytest

from qiqi_delegate.core import build_task_packet
from qiqi_delegate.runtime import DelegateRuntime

def test_standalone_mcp_imports_without_supervisor_or_workspace_template(tmp_path):
    env = dict(os.environ, QIQI_WORKSPACE_ROOT=str(tmp_path))
    result = subprocess.run([
        sys.executable, "-c",
        "from qiqi_delegate.server import mcp; assert hasattr(mcp, 'run')"
    ], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_mcp_initialize_stdio_handshake(tmp_path):
    """Import-only tests miss crashes in MCPServer.run and stdio transport."""
    env = dict(os.environ, QIQI_WORKSPACE_ROOT=str(tmp_path))
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "qiqi-startup-test", "version": "1.0.0"},
        },
    }
    proc = subprocess.run(
        [sys.executable, "-m", "qiqi_delegate.server"],
        input=json.dumps(initialize) + "\n",
        env=env, capture_output=True, text=True, timeout=20,
    )
    replies = []
    for line in proc.stdout.splitlines():
        try:
            response = json.loads(line)
        except ValueError:
            continue
        if response.get("id") == 1:
            replies.append(response)
    assert len(replies) == 1, (
        f"MCP initialize response missing; exit={proc.returncode}; "
        f"stdout={proc.stdout[-2000:]!r}; stderr={proc.stderr[-4000:]!r}"
    )
    assert "result" in replies[0], f"MCP initialize error: {replies[0]!r}"
    assert replies[0]["result"].get("serverInfo", {}).get("name"), replies[0]

def test_direct_native_capture_creates_and_releases_repo_claim(tmp_path, monkeypatch):
    repo = tmp_path / "backend"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (tmp_path / "repos.yaml").write_text(
        "repositories:\n  - name: backend\n    path: backend\n"
    )
    (tmp_path / "agent-routing.yaml").write_text(
        "routes:\n  codex-balanced:\n    agent: codex\n    args: []\n"
    )
    rt = DelegateRuntime(tmp_path)
    monkeypatch.setattr("qiqi_delegate.runtime.shutil.which", lambda _: "/bin/true")
    async def mock_json(*args, **kwargs):
        assert args[:2] == ("workspace", "create")
        return {"workspace": {"workspace_id": "w1"}, "root_pane": {"pane_id": "p1"}}
    async def mock_start(*args, **kwargs):
        return "myagent", {"agent_status": "idle"}
    async def mock_prompt(*args, **kwargs):
        return "done", {
            "agent_status": "done",
            "agent_session": {"kind": "id", "agent": "codex", "value": "native-session-1"}
        }
    async def mock_capture(*args, **kwargs):
        return {"state": "settled", "agent_response": "Native hook result was captured"}
    async def mock_run(*args, **kwargs):
        if args[:2] == ("status", "server"):
            return 0, "", ""
        assert args[:3] == ("workspace", "close", "w1")
        return 0, "", ""
    monkeypatch.setattr(rt, "_json", mock_json)
    monkeypatch.setattr(rt, "_start_agent", mock_start)
    monkeypatch.setattr(rt, "_prompt", mock_prompt)
    monkeypatch.setattr(rt, "_capture", mock_capture)
    monkeypatch.setattr(rt, "_run", mock_run)
    packet = build_task_packet(objective="API implementation", scope=["src"],
                               acceptance_criteria=["HTTP 201"])
    result = asyncio.run(rt.delegate(repository="backend", route="codex-balanced", packet=packet))
    assert result["state"] == "settled"
    assert result["session_id"] == "native-session-1"
    assert result["agent_response"] == "Native hook result was captured"
    with rt._connect() as db:
        assert db.execute("SELECT count(*) FROM turns").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM write_claims").fetchone()[0] == 0
    assert not (repo / ".qiqi").exists()
    assert not (repo / ".herdr-task-mcp").exists()

def test_tool_errors_include_registry_reason_and_repair_action(tmp_path):
    """No generic 'Error executing tool' when a sibling/registry path is invalid."""
    (tmp_path / "repos.yaml").write_text(
        "repositories:\n  - name: frontend\n    path: ../../forbidden\n"
    )
    (tmp_path / "agent-routing.yaml").write_text(
        "routes:\n  codex-balanced:\n    agent: codex\n    args: []\n"
    )
    script = """
import asyncio
from mcp.server.mcpserver.exceptions import ToolError
from qiqi_delegate.server import start_graph, delegate_repo_task, Graph, Node, Packet

packet = Packet(objective='Read frontend', scope=['src'],
                acceptance_criteria=['Describe entry point'])
graph = Graph(nodes=[Node(node_id='front', repository='frontend',
                          route='codex-balanced', task_packet=packet)])
async def verify():
    for label, invocation in (
        ('graph', lambda: start_graph(graph)),
        ('direct', lambda: delegate_repo_task(
            repository='frontend', route='codex-balanced',
            objective='Read frontend', scope=['src'],
            acceptance_criteria=['Describe entry point'])),
    ):
        try:
            await invocation()
        except ToolError as exc:
            message = str(exc)
            assert 'code=repository_registry_invalid' in message, message
            assert 'escapes the workspace parent boundary' in message, message
            assert 'action=' in message, message
        else:
            raise AssertionError(label + ': invalid path unexpectedly accepted')
asyncio.run(verify())
"""
    env = dict(os.environ, QIQI_WORKSPACE_ROOT=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-c", script], env=env,
        capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_herdr_uses_current_socket_by_default_and_named_session_only_when_opted_in(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/herdr-existing.sock")
    monkeypatch.delenv("QIQI_HERDR_SESSION", raising=False)
    current = DelegateRuntime(tmp_path)
    assert current.herdr_session is None
    assert current._herdr_argv("workspace", "list") == [
        "herdr", "workspace", "list"
    ]

    monkeypatch.setenv("QIQI_HERDR_SESSION", "isolated-work")
    dedicated = DelegateRuntime(tmp_path)
    assert dedicated._herdr_argv("workspace", "list") == [
        "herdr", "--session", "isolated-work", "workspace", "list"
    ]

    monkeypatch.setenv("QIQI_HERDR_SESSION", " ")
    with pytest.raises(ValueError, match="non-empty"):
        DelegateRuntime(tmp_path)


def test_herdr_existing_server_reused_without_nested_client_launch(tmp_path, monkeypatch):
    rt = DelegateRuntime(tmp_path)
    calls = []
    async def healthy(*args, **kwargs):
        calls.append(args)
        assert args[:2] == ("status", "server")
        return 0, "", ""
    monkeypatch.setattr(rt, "_run", healthy)
    asyncio.run(rt._ensure_herdr_server())
    assert calls == [("status", "server")]


def test_herdr_missing_server_starts_headless_without_session_attach(tmp_path, monkeypatch):
    monkeypatch.delenv("QIQI_HERDR_SESSION", raising=False)
    rt = DelegateRuntime(tmp_path)
    commands = []
    checks = []
    class FakeServer:
        returncode = None
    async def probe(*args, **kwargs):
        checks.append(args)
        assert args[:2] == ("status", "server")
        return (0 if len(checks) >= 3 else 1), "", ""
    async def launch(*argv, **kwargs):
        commands.append((argv, kwargs))
        return FakeServer()

    monkeypatch.setattr(rt, "_run", probe)
    monkeypatch.setattr("qiqi_delegate.runtime.asyncio.create_subprocess_exec", launch)
    asyncio.run(rt._ensure_herdr_server())
    assert len(checks) == 3
    assert len(commands) == 1
    argv, options = commands[0]
    assert argv == ("herdr", "server")
    assert options["start_new_session"] is True
    assert "attach" not in argv



def test_workspace_info_prevents_unknown_routes_before_graph_persistence(tmp_path):
    repo = tmp_path / "frontend"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (tmp_path / "repos.yaml").write_text(
        "repositories:\n  - name: frontend\n    path: frontend\n"
    )
    (tmp_path / "agent-routing.yaml").write_text(
        "routes:\n  codex-balanced:\n    agent: codex\n    args: []\n"
    )
    script = """
import asyncio
from mcp.server.mcpserver.exceptions import ToolError
from qiqi_delegate.server import workspace_info, start_graph, Graph, Node, Packet

p = Packet(objective="Inspect", scope=["src"],
           acceptance_criteria=["Report entry point"])
def graph(route):
    return Graph(nodes=[Node(node_id="F1", repository="frontend",
                             route=route, task_packet=p)])
async def main():
    info = await workspace_info()
    assert info["repositories"] == ["frontend"], info
    assert info["routes"] == {"codex-balanced": "codex"}, info
    assert info["herdr_session"] == "qiqi-delegate", info
    try:
        await start_graph(graph("explore"))
    except ToolError as exc:
        assert "unknown route: explore" in str(exc), str(exc)
    else:
        raise AssertionError("start_graph accepted nonexistent route")
    result = await start_graph(graph("codex-balanced"))
    assert result["revision"] == 0, result
    assert result["runnable_nodes"] == ["F1"], result
asyncio.run(main())
"""
    env = dict(os.environ, QIQI_WORKSPACE_ROOT=str(tmp_path),
               QIQI_HERDR_SESSION="qiqi-delegate")
    proc = subprocess.run([sys.executable, "-c", script], env=env,
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stdout + proc.stderr



def test_blocked_startup_preserves_workspace_claim_and_diagnostics(tmp_path, monkeypatch):
    """Startup approval must not lose its pane before the user can inspect it."""
    from qiqi_delegate.runtime import AgentStartupBlocked
    repo = tmp_path / "backend"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (tmp_path / "repos.yaml").write_text(
        "repositories:\n  - name: backend\n    path: backend\n"
    )
    (tmp_path / "agent-routing.yaml").write_text(
        "routes:\n  codex-balanced:\n    agent: codex\n    args: []\n"
    )
    rt = DelegateRuntime(tmp_path)
    monkeypatch.setattr("qiqi_delegate.runtime.shutil.which", lambda _: "/bin/true")
    async def ready():
        return None
    async def create(*args, **kwargs):
        assert args[:2] == ("workspace", "create")
        return {"workspace": {"workspace_id": "w-blocked"}, "root_pane": {"pane_id": "p-blocked"}}
    calls = []
    async def commands(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("agent", "start"):
            return 1, "", json.dumps({"error": {
                "code": "agent_not_ready", "message": "blocked during startup"
            }})
        if args[:2] == ("agent", "explain"):
            return 0, '{"status":"blocked","matcher":"approval"}', ""
        if args[:2] == ("workspace", "close"):
            raise AssertionError("must not destroy blocked startup pane")
        raise AssertionError(args)
    monkeypatch.setattr(rt, "_ensure_herdr_server", ready)
    monkeypatch.setattr(rt, "_json", create)
    monkeypatch.setattr(rt, "_run", commands)
    packet = build_task_packet(
        objective="Inspect module", scope=["src"],
        acceptance_criteria=["Explain entrypoint"]
    )
    with pytest.raises(AgentStartupBlocked) as exc:
        asyncio.run(rt.delegate(repository="backend", route="codex-balanced", packet=packet))
    error = str(exc.value)
    assert "agent_not_ready" in error
    assert "workspace_id=w-blocked" in error
    assert "write_claim_id=turn:" in error
    assert "agent explain" in error and "agent read" in error
    assert "release_write_claim" in error
    assert not any(x[:2] == ("workspace", "close") for x in calls)
    with rt._connect() as db:
        claim = db.execute("SELECT claim_id FROM write_claims WHERE repository='backend'").fetchone()
    assert claim is not None and claim[0] in error


def test_graph_review_keeps_startup_failure_details(tmp_path):
    """Lead review needs the exact worker exception, not executor_exception alone."""
    from qiqi_delegate.task_graph_runtime import GraphRuntime, task_graph_from_payload
    from qiqi_delegate.task_graph_store import GraphRuntimeStore
    rt = DelegateRuntime(tmp_path)
    graph = task_graph_from_payload({"nodes": [{
        "node_id": "backend", "repository": "backend", "route": "codex-balanced",
        "task_packet": {"objective": "Inspect", "scope": ["src"],
                        "acceptance_criteria": ["Describe API"]}
    }]})
    gr = GraphRuntime(GraphRuntimeStore(rt.db))
    current = gr.start_graph(graph, repository_names=["backend"])
    async def fail(_node):
        raise RuntimeError("agent_not_ready; workspace_id=w-blocked; write_claim_id=turn:abc")
    with pytest.raises(RuntimeError, match="agent_not_ready"):
        asyncio.run(gr.delegate_next(current["graph_run_id"], executor=fail))
    after = gr.get_graph(current["graph_run_id"])
    assert len(after["review_required"]) == 1
    item = after["review_required"][0]
    review = gr.get_node_review(
        current["graph_run_id"], item["node_id"], item["attempt_id"]
    )
    result = review["result"]
    assert result["failure_type"] == "executor_exception"
    assert result["agent_response"] is None
    assert "write_claim_id=turn:abc" in result["failure_detail"]

