import asyncio
import json
import os
import shlex
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

def test_failed_headless_server_readiness_reaps_detached_process(tmp_path, monkeypatch):
    """A failing status probe after spawn must not leak an orphaned Herdr."""
    rt = DelegateRuntime(tmp_path)
    calls = []

    class Child:
        returncode = None

        def terminate(self):
            calls.append("terminate")

        async def wait(self):
            calls.append("wait")
            self.returncode = -15
            return self.returncode

    child = Child()

    async def spawn(*args, **kwargs):
        calls.append("spawn")
        return child

    async def status(*args, **kwargs):
        assert args[:2] == ("status", "server")
        calls.append("status")
        # First two status checks request the headless launch; the third
        # raises after the detached child exists.
        if calls.count("status") < 3:
            return 1, "", ""
        raise RuntimeError("Herdr command timeout during startup probe")

    monkeypatch.setattr(rt, "_run", status)
    monkeypatch.setattr("qiqi_delegate.runtime.asyncio.create_subprocess_exec", spawn)

    with pytest.raises(RuntimeError, match="timeout during startup probe"):
        asyncio.run(rt._ensure_herdr_server())
    assert calls == [
        "status", "status", "spawn", "status", "terminate", "wait",
    ]


def test_herdr_command_asyncio_timeout_kills_and_reaps_child(tmp_path, monkeypatch):
    """Python 3.10 asyncio.TimeoutError must reach subprocess cleanup."""
    rt = DelegateRuntime(tmp_path)
    class TimedOutChild:
        returncode = None

        def __init__(self):
            self.killed = False
            self.reaped = False

        def kill(self):
            self.killed = True

        async def communicate(self):
            if self.killed:
                self.reaped = True
            return b"", b""

    child = TimedOutChild()

    async def create_child(*argv, **kwargs):
        return child

    async def raise_timeout(task, timeout):
        # Avoid leaving an un-awaited coroutine in this intentionally mocked wait.
        task.close()
        raise asyncio.TimeoutError("synthetic Python 3.10-style wait_for timeout")

    monkeypatch.setattr("qiqi_delegate.runtime.asyncio.create_subprocess_exec", create_child)
    monkeypatch.setattr("qiqi_delegate.runtime.asyncio.wait_for", raise_timeout)

    with pytest.raises(RuntimeError, match="Herdr command timeout"):
        asyncio.run(rt._run("status", "server", timeout=0.001))
    assert child.killed and child.reaped


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
    # Recovery must use the MCP process interpreter, not an unqualified CLI
    # which is absent from PATH under the documented non-activated venv setup.
    assert (
        f"{shlex.quote(sys.executable)} -m qiqi_delegate.maintenance "
        "release-claim" in error
    )
    assert f"--workspace {shlex.quote(str(tmp_path.resolve()))}" in error
    assert "--worker-termination-confirmed" in error
    assert "qiqi-delegate-admin" not in error
    assert not any(x[:2] == ("workspace", "close") for x in calls)
    with rt._connect() as db:
        claim = db.execute("SELECT claim_id FROM write_claims WHERE repository='backend'").fetchone()
    assert claim is not None and claim[0] in error
    assert f"--claim-id {shlex.quote(claim[0])}" in error
    assert exc.value.recovery_command in exc.value.actionable_detail()


def test_long_recovery_command_survives_public_mcp_error(tmp_path, monkeypatch):
    """Clip verbose diagnostics, not exact recovery arguments, on the direct tool."""
    monkeypatch.setenv("QIQI_WORKSPACE_ROOT", str(tmp_path))
    from mcp.server.mcpserver.exceptions import ToolError
    from qiqi_delegate.runtime import AgentStartupBlocked
    from qiqi_delegate.server import _public_tool_errors

    long_workspace = "/tmp/" + "/".join(["it's-a-long-workspace-" * 15] * 6)
    recovery = (
        f"{shlex.quote(sys.executable)} -m qiqi_delegate.maintenance "
        f"release-claim --workspace {shlex.quote(long_workspace)} "
        "--repository backend --claim-id turn:actual "
        "--worker-termination-confirmed"
    )
    blocker = AgentStartupBlocked(
        "qiqi-agent", "pane-id", "startup evidence " * 200,
        recovery_command=recovery,
        public_context="agent_not_ready; inspect: " + "verbose data " * 200,
    )

    @_public_tool_errors
    async def blocked():
        raise blocker

    with pytest.raises(ToolError) as exc:
        asyncio.run(blocked())
    public = str(exc.value)
    assert public.startswith("code=agent_startup_blocked;")
    assert f"recovery_command={recovery}" in public
    assert public.endswith("operator-side claim cleanup before retrying.")
    assert "--repository backend --claim-id turn:actual " in public
    assert "--worker-termination-confirmed" in public
    assert len(public) > 1200
    assert "verbose data " * 100 not in public


def test_graph_review_preserves_long_startup_recovery(tmp_path):
    """Graph results must not tail-truncate a retained claim recovery command."""
    from qiqi_delegate.runtime import AgentStartupBlocked
    from qiqi_delegate.task_graph_runtime import GraphRuntime, task_graph_from_payload
    from qiqi_delegate.task_graph_store import GraphRuntimeStore

    command = (
        f"{shlex.quote(sys.executable)} -m qiqi_delegate.maintenance "
        "release-claim --workspace /tmp/" + "long-workspace-" * 175
        + " --repository backend --claim-id turn:exact --worker-termination-confirmed"
    )
    gr = GraphRuntime(GraphRuntimeStore(tmp_path / "graph.sqlite3"))
    started = gr.start_graph(task_graph_from_payload({"nodes": [{
        "node_id": "backend", "repository": "backend", "route": "codex-balanced",
        "task_packet": {"objective": "Inspect", "scope": ["src"],
                        "acceptance_criteria": ["Describe API"]},
    }]}), repository_names=["backend"])

    async def fail(_node):
        raise AgentStartupBlocked(
            "peer", "pane", "evidence " * 500,
            recovery_command=command,
            public_context="agent_not_ready; " + "detailed evidence " * 400,
        )

    with pytest.raises(AgentStartupBlocked):
        asyncio.run(gr.delegate_next(started["graph_run_id"], executor=fail))
    current = gr.get_graph(started["graph_run_id"])
    loc = current["review_required"][0]
    reviews = gr.get_node_reviews(
        started["graph_run_id"], [(loc["node_id"], loc["attempt_id"])],
        expected_revision=current["revision"],
    )
    detail = reviews["reviews"][0]["result"]["failure_detail"]
    assert f"recovery_command={command}" in detail
    assert detail.endswith("--worker-termination-confirmed")


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
    review = gr.get_node_reviews(
        current["graph_run_id"], [(item["node_id"], item["attempt_id"])],
        expected_revision=after["revision"],
    )
    result = review["reviews"][0]["result"]
    assert result["failure_type"] == "executor_exception"
    assert result["agent_response"] is None
    assert "write_claim_id=turn:abc" in result["failure_detail"]



def test_removed_mcp_tools_are_not_public(tmp_path, monkeypatch):
    """Review batching is the only public review entry point; recovery is not MCP."""
    monkeypatch.setenv("QIQI_WORKSPACE_ROOT", str(tmp_path))
    from qiqi_delegate import server
    assert not hasattr(server, "get_node_review")
    assert not hasattr(server, "release_write_claim")
    assert hasattr(server, "get_node_reviews")
    assert hasattr(server, "submit_decisions")


def test_native_ambiguous_capture_flows_into_graph(tmp_path, monkeypatch):
    """DelegateRuntime and GraphRuntime must agree on the ambiguous result shape."""
    from qiqi_delegate.task_graph_runtime import GraphRuntime, task_graph_from_payload
    from qiqi_delegate.task_graph_store import GraphRuntimeStore

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
        return {"workspace": {"workspace_id": "w1"}, "root_pane": {"pane_id": "p1"}}

    async def start(*args, **kwargs):
        return "agent1", {"agent_status": "idle"}

    async def prompt(*args, **kwargs):
        return "done", {
            "agent_status": "done",
            "agent_session": {"kind": "id", "agent": "codex", "value": "native-1"},
        }

    async def capture(*args, **kwargs):
        return {
            "state": "capture_ambiguous",
            "agent_response": None,
            "candidate_count": 2,
        }

    async def run(*args, **kwargs):
        assert args[:3] == ("workspace", "close", "w1")
        return 0, "", ""

    monkeypatch.setattr(rt, "_ensure_herdr_server", ready)
    monkeypatch.setattr(rt, "_json", create)
    monkeypatch.setattr(rt, "_start_agent", start)
    monkeypatch.setattr(rt, "_prompt", prompt)
    monkeypatch.setattr(rt, "_capture", capture)
    monkeypatch.setattr(rt, "_run", run)

    graph = task_graph_from_payload({"nodes": [{
        "node_id": "B1", "repository": "backend", "route": "codex-balanced",
        "task_packet": {"objective": "Inspect", "scope": ["src"],
                        "acceptance_criteria": ["Describe API"]},
    }]})
    gr = GraphRuntime(GraphRuntimeStore(rt.db))
    started = gr.start_graph(graph, repository_names=["backend"])

    async def execute(node):
        return await rt.delegate(
            repository=node.repository, route=node.route, packet=node.task_packet
        )

    output = asyncio.run(gr.delegate_next(started["graph_run_id"], executor=execute))
    assert output["graph_state"] == "awaiting_review"
    assert output["results"][0]["runtime_state"] == "capture_ambiguous"
    assert output["review_required"][0]["candidate_count"] == 2
    with rt._connect() as db:
        assert db.execute("SELECT count(*) FROM turns").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM write_claims").fetchone()[0] == 0
