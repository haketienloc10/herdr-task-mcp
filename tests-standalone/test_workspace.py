import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from qiqi_delegate.install import START, END, RULES, managed_rules, install_workspace
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

def test_managed_rules_are_generic_and_forbid_lead_direct_repo_access():
    # The installer must not prescribe a particular repository layout or demo name.
    for demo_specific in ("frontend", "backend", "../", "herdr-delegate-lab"):
        assert demo_specific not in RULES
    assert "Lead không tự đọc, sửa, chạy lệnh, kiểm thử hoặc commit" in RULES
    assert "Giao mọi công việc repository-scoped cho Peer" in RULES
    assert "Không tự suy đoán đường dẫn, tạo, clone" in RULES
    assert "Không tự cài MCP, thêm rule hoặc sửa `AGENTS.md` trong repository đích" in RULES
    assert "không đồng nghĩa với ACCEPT" in RULES

    # Updating an existing managed block must preserve both surrounding regions.
    prefix = b"# Global workspace rules\r\n\r\nDo not touch.  \r\n"
    suffix = b"\r\n# Other agent policies\r\nUnchanged trailing policy.  \r\n"
    previous = prefix + START.encode() + b"\r\nOld managed content\r\n" + END.encode() + suffix
    new = managed_rules(previous)
    assert new.startswith(prefix)
    assert new.endswith(suffix)
    assert b"Old managed content" not in new
    assert b"Lead kh" in new
    assert managed_rules(new) == new


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
    assert first["python"] == os.path.abspath(sys.executable)
    asyncio.run(install_workspace(root))
    assert (root / "AGENTS.md").read_bytes() == agents
    assert (root / ".codex" / "config.toml").read_text() == toml
    assert tuple((root / "frontend").rglob("*")) == previous_front
    assert "repositories: []" in (root / "repos.yaml").read_text()

def test_installer_enables_default_agent_flags_without_overwriting_existing_routes(tmp_path: Path):
    """A new workspace receives both defaults; rerunning preserves user overrides."""
    asyncio.run(install_workspace(tmp_path))
    rt = DelegateRuntime(tmp_path)
    assert rt.route("codex-balanced") == ("codex", ["--yolo"])
    assert rt.route("claude-balanced") == (
        "claude", ["--permission-mode", "auto"]
    )

    routing = tmp_path / "agent-routing.yaml"
    custom = (
        "routes:\n"
        "  codex-balanced:\n"
        "    agent: codex\n"
        "    args: []\n"
        "  claude-balanced:\n"
        "    agent: claude\n"
        "    args: []\n"
    )
    routing.write_text(custom)
    asyncio.run(install_workspace(tmp_path))
    assert routing.read_text() == custom
    assert rt.route("codex-balanced") == ("codex", [])
    assert rt.route("claude-balanced") == ("claude", [])


def test_installer_preserves_uv_venv_python_symlink_and_repairs_prior_config(tmp_path: Path):
    """A venv Python symlink must not be resolved to uv's base interpreter."""
    venv_bin = tmp_path / "tools" / "qiqi-venv" / "bin"
    venv_bin.mkdir(parents=True)
    interpreter = venv_bin / "python"
    interpreter.symlink_to(Path(sys.executable).resolve())

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = b"# Project-owned rules\r\n\r\nDo not change. \r\n"
    (workspace / "AGENTS.md").write_bytes(original)

    # Model a previously installed broken registration using the uv-managed base.
    asyncio.run(install_workspace(workspace, python=Path(sys.executable).resolve()))
    before = (workspace / "AGENTS.md").read_bytes()
    old_codex = tomllib.loads((workspace / ".codex" / "config.toml").read_text())
    assert old_codex["mcp_servers"]["qiqi_delegate"]["command"] != str(interpreter)

    info = asyncio.run(install_workspace(workspace, python=interpreter))
    assert info["python"] == str(interpreter)
    assert (workspace / "AGENTS.md").read_bytes() == before
    assert before.startswith(original)

    new_codex = tomllib.loads((workspace / ".codex" / "config.toml").read_text())
    codex = new_codex["mcp_servers"]["qiqi_delegate"]
    assert codex["command"] == str(interpreter)
    assert codex["env"]["QIQI_WORKSPACE_ROOT"] == str(workspace)
    claude = json.loads((workspace / ".mcp.json").read_text())
    assert claude["mcpServers"]["qiqi_delegate"]["command"] == str(interpreter)

    # Venv identity remains even though physical interpreter target is elsewhere.
    assert interpreter.resolve() != interpreter
    asyncio.run(install_workspace(workspace, python=interpreter))
    assert (workspace / "AGENTS.md").read_bytes() == before


def test_installer_sets_named_herdr_session_in_project_configs(tmp_path: Path):
    original = b"# Existing workspace rules\\r\\nOnly modify owned marker.  \\r\\n"
    # Use CRLF in the user-controlled rules to verify exact preservation.
    original = original.replace(b"\\\\r", b"\\r").replace(b"\\\\n", b"\\n")
    (tmp_path / "AGENTS.md").write_bytes(original)

    info = asyncio.run(install_workspace(tmp_path, herdr_session="qiqi-delegate"))
    assert info["herdr_session"] == "qiqi-delegate"

    codex = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text())
    codex_server = codex["mcp_servers"]["qiqi_delegate"]
    assert codex_server["env"]["QIQI_HERDR_SESSION"] == "qiqi-delegate"
    assert codex_server["env"]["QIQI_WORKSPACE_ROOT"] == str(tmp_path)
    claude = json.loads((tmp_path / ".mcp.json").read_text())
    assert claude["mcpServers"]["qiqi_delegate"]["env"]["QIQI_HERDR_SESSION"] == "qiqi-delegate"

    before = (tmp_path / "AGENTS.md").read_bytes()
    assert before.startswith(original)
    # Reinstall without an explicit flag: project-specific selection is durable.
    retained = asyncio.run(install_workspace(tmp_path))
    assert retained["herdr_session"] == "qiqi-delegate"
    assert (tmp_path / "AGENTS.md").read_bytes() == before
    assert "qiqi-delegate" in (tmp_path / ".codex" / "config.toml").read_text()


@pytest.mark.parametrize("invalid", ["", ".", "..", "../escape", "bad name", "a" * 65])
def test_installer_rejects_invalid_session_before_any_write(tmp_path: Path, invalid: str):
    original = b"# Existing workspace policy\\n"
    (tmp_path / "AGENTS.md").write_bytes(original)
    with pytest.raises(ValueError, match="Herdr session"):
        asyncio.run(install_workspace(tmp_path, herdr_session=invalid))
    assert (tmp_path / "AGENTS.md").read_bytes() == original
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".mcp.json").exists()


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

def test_registered_sibling_git_roots_are_supported_without_child_install(tmp_path: Path):
    """MCP's control workspace is a sibling of its execution Git roots."""
    workspace = tmp_path / "herdr-delegate-lab"
    workspace.mkdir()
    frontend = tmp_path / "frontend"
    backend = tmp_path / "backend"
    setup_git(frontend)
    setup_git(backend)
    (workspace / "repos.yaml").write_text(
        "repositories:\n  - name: frontend\n    path: ../frontend\n"
        "  - name: backend\n    path: ../backend\n"
    )
    (workspace / "agent-routing.yaml").write_text(
        "routes:\n  codex-balanced:\n    agent: codex\n    args: []\n"
    )

    rt = DelegateRuntime(workspace)
    assert rt.repos() == {"frontend": frontend.resolve(), "backend": backend.resolve()}
    assert rt.route("codex-balanced") == ("codex", [])
    assert not (frontend / ".herdr-task-mcp").exists()
    assert not (backend / ".herdr-task-mcp").exists()

    from qiqi_delegate.task_graph_runtime import GraphRuntime, task_graph_from_payload
    from qiqi_delegate.task_graph_store import GraphRuntimeStore
    graph = task_graph_from_payload({"nodes": [
        {"node_id": "front", "repository": "frontend", "route": "codex-balanced",
         "task_packet": {"objective": "Read frontend", "scope": ["src"],
                         "acceptance_criteria": ["Document verified findings"]}},
        {"node_id": "back", "repository": "backend", "route": "codex-balanced",
         "task_packet": {"objective": "Read backend", "scope": ["src"],
                         "acceptance_criteria": ["Document verified findings"]}}
    ]})
    initial = GraphRuntime(GraphRuntimeStore(rt.db)).start_graph(
        graph, repository_names=rt.repos().keys()
    )
    assert set(initial["runnable_nodes"]) == {"front", "back"}


def test_repository_registry_rejects_duplicate_git_root_aliases(tmp_path: Path):
    """Logical aliases must not permit concurrent writers to one worktree."""
    workspace = tmp_path / "control"
    workspace.mkdir()
    repo = tmp_path / "frontend"
    setup_git(repo)
    # Both spellings point to the identical canonical Git root.
    (workspace / "repos.yaml").write_text(
        "repositories:\n"
        "  - name: frontend\n    path: ../frontend\n"
        "  - name: frontend-alias\n    path: .././frontend\n"
    )
    rt = DelegateRuntime(workspace)
    with pytest.raises(ValueError, match="same Git root"):
        rt.repos()
    # A symlink inside the permitted parent boundary must also be rejected.
    (tmp_path / "frontend-link").symlink_to(repo, target_is_directory=True)
    (workspace / "repos.yaml").write_text(
        "repositories:\n"
        "  - name: frontend\n    path: ../frontend\n"
        "  - name: frontend-link\n    path: ../frontend-link\n"
    )
    with pytest.raises(ValueError, match="same Git root"):
        rt.repos()


def test_repository_registry_rejects_parent_escape_and_symlink(tmp_path: Path):
    scope = tmp_path / "project"
    scope.mkdir()
    workspace = scope / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    setup_git(outside)
    (workspace / "repos.yaml").write_text(
        "repositories:\n  - name: invalid\n    path: ../../outside\n"
    )
    rt = DelegateRuntime(workspace)
    with pytest.raises(ValueError, match="escapes the workspace parent boundary"):
        rt.repos()

    (scope / "linked").symlink_to(outside, target_is_directory=True)
    (workspace / "repos.yaml").write_text(
        "repositories:\n  - name: invalid\n    path: ../linked\n"
    )
    with pytest.raises(ValueError, match="escapes the workspace parent boundary"):
        rt.repos()


def test_no_supervisor_modules_in_package():
    import qiqi_delegate
    root = Path(qiqi_delegate.__file__).parent
    assert not list(root.glob("*supervisor*"))
