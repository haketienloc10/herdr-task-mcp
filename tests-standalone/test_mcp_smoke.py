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
