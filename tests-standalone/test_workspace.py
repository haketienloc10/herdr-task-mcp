import asyncio
import json
import subprocess
import sys
from pathlib import Path
import pytest

from qiqi_delegate.install import START, END, managed_rules, install_workspace
from qiqi_delegate.runtime import DelegateRuntime

def setup_git(path: Path):
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)

def test_agent_markers_are_byte_for_byte_bounded():
    initial = b"# User rules\r\n\r\nKeep existing rule.  \r\n"
    updated = managed_rules(initial)
    assert updated.startswith(initial)
    assert updated.count(START.encode()) == updated.count(END.encode()) == 1
    assert managed_rules(updated) == updated
    before, after = updated.split(START.encode(), 1)[0], updated.split(END.encode(), 1)[1]
    modified = updated.replace(b"Lead", b"Coordinator")
    assert managed_rules(modified).startswith(before)
    assert managed_rules(modified).endswith(after)

@pytest.mark.parametrize("broken", [
    b"<!-- BEGIN HERDR-TASK-MCP RULES -->\n",
    b"<!-- END HERDR-TASK-MCP RULES -->\n",
    b"<!-- BEGIN HERDR-TASK-MCP RULES -->\n<!-- BEGIN HERDR-TASK-MCP RULES -->\n<!-- END HERDR-TASK-MCP RULES -->",
    b"<!-- END HERDR-TASK-MCP RULES -->\n<!-- BEGIN HERDR-TASK-MCP RULES -->",
])
def test_invalid_markers_fail_closed(broken):
    with pytest.raises(ValueError, match="marker"):
        managed_rules(broken)

def test_install_workspace_preserves_unrelated_agent_rules_and_mcp(tmp_path: Path):
    root = tmp_path
    original = b"# Existing project instructions\n\nNever change this section.\n"
    (root / "AGENTS.md").write_bytes(original)
    (root / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"other": {"command": "other", "args": []}},
        "unrelated": {"keep": True}
    }))
    (root / ".codex").mkdir()
    (root / ".codex" / "config.toml").write_text(
        'model = "gpt-6"\n[features.code_mode]\ndirect_only_tool_namespaces = ["mcp__other"]\n'
    )
    setup_git(root / "frontend")
    setup_git(root / "backend")
    previous_front = tuple((root / "frontend").rglob("*"))
    first = asyncio.run(install_workspace(root))
    agents = (root / "AGENTS.md").read_bytes()
    assert agents.startswith(original)
    assert agents.count(START.encode()) == 1
    assert first["mcp"] == "qiqi_delegate"
    toml = (root / ".codex" / "config.toml").read_text()
    assert "mcp__qiqi_delegate" in toml and "mcp__other" in toml
    claude = json.loads((root / ".mcp.json").read_text())
    assert "other" in claude["mcpServers"]
    assert claude["unrelated"] == {"keep": True}
    assert first["python"] == str(Path(sys.executable).resolve())
    asyncio.run(install_workspace(root))
    assert (root / "AGENTS.md").read_bytes() == agents
    assert (root / ".codex" / "config.toml").read_text() == toml
    assert tuple((root / "frontend").rglob("*")) == previous_front
    assert "repositories: []" in (root / "repos.yaml").read_text()

def test_invalid_rules_abort_before_any_other_changes(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text(START + "\n")
    with pytest.raises(ValueError, match="marker"):
        asyncio.run(install_workspace(tmp_path))
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".codex").exists()

def test_invalid_existing_codex_fails_before_modifying_agents(tmp_path: Path):
    agent_bytes = b"# Keep me\n"
    (tmp_path / "AGENTS.md").write_bytes(agent_bytes)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("[bad\n")
    with pytest.raises(ValueError, match="invalid TOML"):
        asyncio.run(install_workspace(tmp_path))
    assert (tmp_path / "AGENTS.md").read_bytes() == agent_bytes

def test_runtime_repo_registry_no_child_install(tmp_path: Path):
    setup_git(tmp_path / "frontend")
    setup_git(tmp_path / "backend")
    (tmp_path / "repos.yaml").write_text(
        "repositories:\n  - name: frontend\n    path: frontend\n  - name: backend\n    path: backend\n"
    )
    (tmp_path / "agent-routing.yaml").write_text(
        "routes:\n  claude-balanced:\n    agent: claude\n    args: ['--permission-mode', 'auto']\n"
    )
    rt = DelegateRuntime(tmp_path)
    assert set(rt.repos()) == {"frontend", "backend"}
    assert rt.route("claude-balanced") == ("claude", ["--permission-mode", "auto"])
    claim_id = "turn:test"
    rt._claim("backend", claim_id)
    with pytest.raises(RuntimeError, match="busy"):
        rt._claim("backend", "turn:other")
    assert rt.release_claim("backend", claim_id)
    assert rt.release_claim("backend", claim_id) is False
    assert not (tmp_path / "backend" / ".qiqi").exists()
    assert not (tmp_path / "frontend" / ".herdr-task-mcp").exists()

def test_no_supervisor_modules_in_package():
    import qiqi_delegate
    root = Path(qiqi_delegate.__file__).parent
    assert not list(root.glob("*supervisor*"))
