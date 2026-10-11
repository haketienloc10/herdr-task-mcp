import asyncio
import ast
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

from qiqi_delegate.install import (
    START, END, CLAUDE_START, CLAUDE_END, RULES,
    managed_rules, managed_claude_rules, install_workspace,
)
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


def test_installed_rules_keep_one_task_schema_and_guide_task_specific_criteria():
    installed = managed_rules(b"").decode("utf-8")
    assert "một schema `TaskPacket`" in installed
    for task_kind in ("analysis/discovery/review", "coding", "testing", "documentation"):
        assert f"- Với {task_kind}:" in installed
    assert "không sửa file" in installed
    assert "test đã chạy" in installed
    assert "test chưa chạy" in installed
    assert "không bịa đặt tính năng" in installed


def test_task_type_policy_stays_aligned_across_three_surfaces():
    # server.py initializes its runtime at import time; inspect static MCP instructions.
    root = Path(__file__).resolve().parents[1]
    server_ast = ast.parse((root / "qiqi_delegate" / "server.py").read_text(encoding="utf-8"))
    mcp_call = next(
        node.value for node in server_ast.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "mcp" for target in node.targets)
    )
    assert isinstance(mcp_call, ast.Call)
    instructions = ast.literal_eval(
        next(keyword.value for keyword in mcp_call.keywords if keyword.arg == "instructions")
    )
    readme = (root / "README.md").read_text(encoding="utf-8")
    readme_policy = readme.split("### Chính sách giao việc theo loại nhiệm vụ", 1)[1].split("\n## ", 1)[0]

    # Each entry captures a task-specific obligation, not just the task name.
    # Check it independently on every published policy surface.
    expected = {
        "installed AGENTS.md": {
            "schema": ("một schema `TaskPacket`",),
            "proportional criteria": ("Chọn `acceptance_criteria` và `constraints` theo mục tiêu thực tế",),
            "analysis": ("Với analysis/discovery/review: chỉ chọn các tiêu chí áp dụng",
                         "task tra cứu đơn giản chỉ cần câu trả lời với bằng chứng trực tiếp"),
            "read-only": ("Nếu task chỉ đọc, ghi rõ không sửa file trong `constraints`",),
            "coding": ("Với coding: nêu hành vi cần triển khai, phạm vi thay đổi, contract phải giữ",
                       "tiêu chí kiểm thử liên quan; Peer báo cáo file thay đổi, test đã chạy"),
            "testing": ("Với testing: xác định hành vi/test cases cần kiểm tra, việc tạo hoặc sửa test",
                        "cách báo cáo pass/fail, lỗi, test chưa chạy; không suy diễn pass khi chưa thực thi"),
            "documentation": ("Với documentation: yêu cầu nội dung khớp implementation/contract đã xác minh",
                              "ví dụ đúng thực tế và các thay đổi tài liệu cụ thể"),
            "mixed work": ("Với task kết hợp nhiều loại việc, ghép các tiêu chí liên quan",),
            "simple work": ("không áp checklist không liên quan hoặc báo cáo dài cho nhiệm vụ đơn giản",),
        },
        "MCP instructions": {
            "schema": ("Use one TaskPacket schema",),
            "proportional criteria": ("tailor acceptance_criteria and constraints to the objective",),
            "analysis": ("For analysis/discovery/review request only applicable evidence and explanations",
                         "simple lookups require only direct evidence, not a full system trace"),
            "read-only": ("For read-only tasks explicitly prohibit file changes",),
            "coding": ("For coding require scoped changes, contract compatibility and relevant test evidence",),
            "testing": ("For testing specify cases, permitted test edits/execution, pass/fail results",
                        "and unrun cases; never imply an unrun test passed"),
            "documentation": ("For documentation require implementation-grounded claims and accurate examples",),
            "mixed work": ("including mixed tasks",),
            "simple work": ("Avoid irrelevant criteria and verbose reports for simple tasks",),
        },
        "README policy": {
            "schema": ("một schema `TaskPacket`",),
            "proportional criteria": ("Lead chọn nội dung `objective`, `scope`, `acceptance_criteria` và `constraints` theo mục tiêu",),
            "analysis": ("| Analysis / Discovery / Review | Chỉ yêu cầu nội dung liên quan đến mục tiêu",
                         "Với task đơn giản như xác nhận vị trí một khóa cấu hình"),
            "read-only": ("Với task chỉ đọc, đặt `constraints` cấm sửa file",),
            "coding": ("| Coding | Mô tả hành vi phải triển khai, phạm vi thay đổi, contract cần giữ, test liên quan",
                       "phân biệt test đã chạy và chưa chạy"),
            "testing": ("| Testing | Nêu test cases, trường hợp biên, kết quả pass/fail và test chưa chạy",
                        "Xác định rõ có được thêm/sửa test và chạy lệnh kiểm thử hay không"),
            "documentation": ("| Documentation | Đối chiếu implementation/contract thực tế",
                              "yêu cầu tài liệu và ví dụ chính xác"),
            "mixed work": ("Với task kết hợp, chỉ ghép các tiêu chí thực sự liên quan",),
            "simple work": ("Không áp checklist đầy đủ cho mọi task",),
        },
    }
    surfaces = {
        "installed AGENTS.md": managed_rules(b"").decode("utf-8"),
        "MCP instructions": instructions,
        "README policy": readme_policy,
    }
    invariant_names = set(expected["installed AGENTS.md"])
    assert all(set(checks) == invariant_names for checks in expected.values())
    for surface, checks in expected.items():
        normalized = " ".join(surfaces[surface].casefold().split())
        for invariant, clauses in checks.items():
            for clause in clauses:
                assert " ".join(clause.casefold().split()) in normalized, (
                    f"Missing {invariant} on {surface}: {clause}"
                )


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
    (root / "CLAUDE.md").write_bytes(b"# Existing Claude instructions\r\n\r\nKeep this.\r\n")
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
    claude_rules = (root / "CLAUDE.md").read_bytes()
    assert claude_rules.startswith(b"# Existing Claude instructions\r\n\r\nKeep this.\r\n")
    assert claude_rules.count(b"@AGENTS.md") == 1
    assert claude_rules.count(CLAUDE_START.encode()) == 1
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
    assert (root / "CLAUDE.md").read_bytes() == claude_rules
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
    from qiqi_delegate.evaluator_routes import CLAUDE_EVALUATOR_ARGS
    assert rt.route("codex-evaluator") == ("codex", ["--sandbox", "read-only"])
    assert rt.route("claude-evaluator") == ("claude", list(CLAUDE_EVALUATOR_ARGS))

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


@pytest.mark.parametrize("failure", ["CLAUDE.md", ".mcp.json", ".herdr-task-mcp/.gitignore"])
def test_install_rolls_back_all_files_when_later_replace_fails(tmp_path, monkeypatch, failure):
    # All old files must be restored, even when the failure occurs after
    # earlier atomic replacements completed.
    agent = tmp_path / "AGENTS.md"
    lead = tmp_path / "CLAUDE.md"
    agent.write_bytes(b"# existing AGENTS policy\n")
    lead.write_bytes(b"# existing CLAUDE policy\n")
    old_agent, old_lead = agent.read_bytes(), lead.read_bytes()
    import qiqi_delegate.install as installer
    real_replace = installer.os.replace
    failed = False

    def failing_replace(source, destination):
        nonlocal failed
        if Path(destination) == tmp_path / failure and not failed:
            failed = True
            raise OSError("injected I/O failure during replace")
        return real_replace(source, destination)

    monkeypatch.setattr(installer.os, "replace", failing_replace)
    with pytest.raises(OSError, match="injected I/O failure"):
        asyncio.run(install_workspace(tmp_path))
    assert failed
    assert agent.read_bytes() == old_agent
    assert lead.read_bytes() == old_lead
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".herdr-task-mcp").exists()
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / "repos.yaml").exists()
    assert not (tmp_path / "agent-routing.yaml").exists()
    assert not list(tmp_path.glob(".qiqi-install-*"))


def test_install_restore_existing_codex_and_claude_config_on_last_replace_failure(tmp_path, monkeypatch):
    # A failed update to an existing workspace must retain every prior byte.
    asyncio.run(install_workspace(tmp_path))
    originals = {
        name: (tmp_path / name).read_bytes()
        for name in ("AGENTS.md", "CLAUDE.md", ".mcp.json",
                     ".codex/config.toml", ".herdr-task-mcp/.gitignore")
    }
    import qiqi_delegate.install as installer
    real_replace = installer.os.replace
    failed = False

    def failing_replace(source, destination):
        nonlocal failed
        if Path(destination) == tmp_path / ".mcp.json" and not failed:
            failed = True
            raise OSError("simulated ENOSPC")
        return real_replace(source, destination)

    # Force updates to all four managed files so the rollback needs backups.
    monkeypatch.setattr(installer.os, "replace", failing_replace)
    with pytest.raises(OSError, match="simulated ENOSPC"):
        asyncio.run(install_workspace(tmp_path, herdr_session="different-session"))
    assert failed
    for name, old in originals.items():
        assert (tmp_path / name).read_bytes() == old
    assert not list(tmp_path.glob(".qiqi-install-*"))


def test_install_refuses_readonly_claude_before_any_write(tmp_path):
    agent = tmp_path / "AGENTS.md"
    lead = tmp_path / "CLAUDE.md"
    agent.write_bytes(b"# preserved AGENTS\n")
    lead.write_bytes(b"# preserved CLAUDE\n")
    lead.chmod(0o444)
    with pytest.raises(PermissionError, match="read-only"):
        asyncio.run(install_workspace(tmp_path))
    assert agent.read_bytes() == b"# preserved AGENTS\n"
    assert lead.read_bytes() == b"# preserved CLAUDE\n"
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".herdr-task-mcp").exists()
    assert not list(tmp_path.glob(".qiqi-install-*"))


def test_install_stage_failure_leaves_files_and_directories_intact(tmp_path, monkeypatch):
    agent = tmp_path / "AGENTS.md"
    agent.write_bytes(b"# Existing shared Lead policy\n")
    import qiqi_delegate.install as installer
    stage = installer._stage_install_file

    def failing_stage(root, content, mode=None):
        if b"<!-- BEGIN HERDR-TASK-MCP CLAUDE IMPORT -->" in content:
            raise OSError("simulated ENOSPC while staging")
        return stage(root, content, mode)

    monkeypatch.setattr(installer, "_stage_install_file", failing_stage)
    with pytest.raises(OSError, match="simulated ENOSPC while staging"):
        asyncio.run(install_workspace(tmp_path))
    assert agent.read_bytes() == b"# Existing shared Lead policy\n"
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".herdr-task-mcp").exists()
    assert not list(tmp_path.glob(".qiqi-install-*"))


def _test_install_xattr(path: Path) -> str:
    if not all(hasattr(os, attr) for attr in ("setxattr", "getxattr", "listxattr")):
        pytest.skip("POSIX xattrs unavailable")
    name = "user.qiqi_install_regression"
    try:
        os.setxattr(path, name, b"owner-policy")
    except OSError:
        pytest.skip("test filesystem does not support user xattrs")
    return name


def test_successful_install_preserves_existing_owner_group_mode_and_xattrs(tmp_path):
    agent = tmp_path / "AGENTS.md"
    agent.write_bytes(b"# Original rules\n")
    agent.chmod(0o640)
    attr = _test_install_xattr(agent)
    original = agent.stat()
    asyncio.run(install_workspace(tmp_path))
    updated = agent.stat()
    assert (updated.st_uid, updated.st_gid) == (original.st_uid, original.st_gid)
    assert updated.st_mode & 0o7777 == original.st_mode & 0o7777
    assert os.getxattr(agent, attr) == b"owner-policy"
    assert not list(tmp_path.glob(".qiqi-install-*"))


def test_rollback_restores_exact_original_inode_and_xattrs(tmp_path, monkeypatch):
    agent = tmp_path / "AGENTS.md"
    lead = tmp_path / "CLAUDE.md"
    agent.write_bytes(b"# Original agents\n")
    lead.write_bytes(b"# Original Claude\n")
    agent.chmod(0o640)
    attr = _test_install_xattr(agent)
    before = {p: (p.read_bytes(), p.stat()) for p in (agent, lead)}
    import qiqi_delegate.install as installer
    real_replace = installer.os.replace

    def fail_after_replacements(source, destination):
        if Path(destination) == tmp_path / ".mcp.json":
            raise OSError("later replacement failed")
        return real_replace(source, destination)

    monkeypatch.setattr(installer.os, "replace", fail_after_replacements)
    with pytest.raises(OSError, match="later replacement failed"):
        asyncio.run(install_workspace(tmp_path))
    for path, (bytes_before, metadata_before) in before.items():
        now = path.stat()
        assert path.read_bytes() == bytes_before
        assert now.st_ino == metadata_before.st_ino
        assert (now.st_uid, now.st_gid, now.st_mode & 0o7777) == (
            metadata_before.st_uid, metadata_before.st_gid, metadata_before.st_mode & 0o7777
        )
    assert os.getxattr(agent, attr) == b"owner-policy"
    assert not (tmp_path / ".codex").exists()
    assert not list(tmp_path.glob(".qiqi-install-*"))


def test_installer_refuses_uncopyable_xattrs_before_modifying_workspace(tmp_path, monkeypatch):
    agent = tmp_path / "AGENTS.md"
    agent.write_bytes(b"# Original agents\n")
    attr = _test_install_xattr(agent)
    before = agent.read_bytes(), agent.stat().st_ino
    import qiqi_delegate.install as installer
    real_setxattr = installer.os.setxattr

    def denied_xattr(path, name, value, *args, **kwargs):
        if Path(path).name.startswith(".qiqi-install-"):
            raise PermissionError("injected ACL/xattr copy failure")
        return real_setxattr(path, name, value, *args, **kwargs)

    monkeypatch.setattr(installer.os, "setxattr", denied_xattr)
    with pytest.raises(PermissionError, match="injected ACL/xattr copy failure"):
        asyncio.run(install_workspace(tmp_path))
    assert agent.read_bytes() == before[0]
    assert agent.stat().st_ino == before[1]
    assert os.getxattr(agent, attr) == b"owner-policy"
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not list(tmp_path.glob(".qiqi-install-*"))


def test_installer_refuses_unverifiable_metadata_before_modifying_workspace(tmp_path, monkeypatch):
    agent = tmp_path / "AGENTS.md"
    agent.write_bytes(b"# Original agents\n")
    import qiqi_delegate.install as installer
    monkeypatch.delattr(installer.os, "listxattr", raising=False)
    with pytest.raises(ValueError, match="cannot safely preserve file xattrs"):
        asyncio.run(install_workspace(tmp_path))
    assert agent.read_bytes() == b"# Original agents\n"
    assert not (tmp_path / ".codex").exists()
    assert not list(tmp_path.glob(".qiqi-install-*"))


@pytest.mark.parametrize("symlink_target", ["regular", "dangling"])
def test_managed_gitignore_symlink_aborts_before_any_workspace_change(tmp_path, symlink_target):
    state = tmp_path / ".herdr-task-mcp"
    state.mkdir()
    target = tmp_path / "user-gitignore"
    if symlink_target == "regular":
        target.write_bytes(b"user-owned contents\n")
    link = state / ".gitignore"
    link.symlink_to(target)
    (tmp_path / "AGENTS.md").write_bytes(b"# untouched user policy\n")
    before = (tmp_path / "AGENTS.md").read_bytes()

    with pytest.raises(ValueError, match="refusing symlink"):
        asyncio.run(install_workspace(tmp_path))
    assert link.is_symlink()
    assert link.readlink() == target
    if symlink_target == "regular":
        assert target.read_bytes() == b"user-owned contents\n"
    else:
        assert not target.exists()
    assert (tmp_path / "AGENTS.md").read_bytes() == before
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".mcp.json").exists()
    assert not list(tmp_path.rglob(".qiqi-install-*"))


@pytest.mark.parametrize("symlink_name", ["repos.yaml", "agent-routing.yaml"])
def test_existing_default_config_symlink_is_not_touched(tmp_path, symlink_name):
    user_file = tmp_path / "user-file"
    user_file.write_bytes(b"# preserved\n")
    link = tmp_path / symlink_name
    link.symlink_to(user_file)
    with pytest.raises(ValueError, match="refusing symlink"):
        asyncio.run(install_workspace(tmp_path))
    assert link.is_symlink()
    assert user_file.read_bytes() == b"# preserved\n"
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".codex").exists()


def test_new_root_files_match_direct_write_permissions(tmp_path):
    reference = tmp_path / "reference.txt"
    reference.write_bytes(b"reference\n")
    reference_mode = reference.stat().st_mode & 0o7777
    reference_group = reference.stat().st_gid
    asyncio.run(install_workspace(tmp_path))
    for filename in ("AGENTS.md", "CLAUDE.md", ".mcp.json",
                     "repos.yaml", "agent-routing.yaml"):
        target = tmp_path / filename
        assert target.stat().st_mode & 0o7777 == reference_mode
        assert target.stat().st_gid == reference_group
    reference.unlink()


def test_new_codex_config_inherits_parent_setgid_metadata(tmp_path):
    parent = tmp_path / ".codex"
    parent.mkdir()
    parent.chmod(0o2770)
    control = parent / "reference.toml"
    control.write_bytes(b"reference\n")
    expected = control.stat()

    asyncio.run(install_workspace(tmp_path))
    config = (parent / "config.toml").stat()
    assert config.st_gid == expected.st_gid
    assert config.st_mode & 0o7777 == expected.st_mode & 0o7777
    assert not list(tmp_path.rglob(".qiqi-install-*"))


def test_stage_failure_in_new_directory_cleans_temporary_files(tmp_path, monkeypatch):
    import qiqi_delegate.install as installer
    real_stage = installer._stage_install_file

    def fail_late(directory, content, mode=None):
        if content == b"*\n!.gitignore\n":
            raise OSError("injected stage failure for gitignore")
        return real_stage(directory, content, mode)

    monkeypatch.setattr(installer, "_stage_install_file", fail_late)
    with pytest.raises(OSError, match="injected stage failure for gitignore"):
        asyncio.run(install_workspace(tmp_path))
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".herdr-task-mcp").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not list(tmp_path.rglob(".qiqi-install-*"))


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



@pytest.mark.parametrize("example", [
    b"# Example in a fenced code block\n```md\n@AGENTS.md\n```\n",
    b"~~~markdown\n@AGENTS.md\n~~~\n",
    b"````md\n@AGENTS.md\n````\n",
    b"    @AGENTS.md\n",
    b"<!--\n@AGENTS.md\n-->\n",
    b"# Example\r\n```md\r\n@AGENTS.md\r\n```\r\n",
])
def test_claude_import_examples_do_not_suppress_managed_import(example):
    installed = managed_claude_rules(example)
    assert installed.startswith(example)
    assert installed.count(b"@AGENTS.md") == 2  # One example, one active import
    assert installed.count(CLAUDE_START.encode()) == 1
    assert installed.count(CLAUDE_END.encode()) == 1
    assert managed_claude_rules(installed) == installed


def test_claude_active_import_outside_fenced_example_needs_no_extra_block():
    existing = b"```md\n@AGENTS.md\n```\n\n@AGENTS.md\n"
    assert managed_claude_rules(existing) == existing


@pytest.mark.parametrize("before,after", [
    (b"```markdown\n", b"\n```\n"),
    (b"~~~markdown\n", b"\n~~~\n"),
    (b"````markdown\n", b"\n````\n"),
    (b"<!--\n", b"\n-->\n"),
    (b"<!--\r\n", b"\r\n-->\r\n"),
])
def test_managed_claude_import_inside_code_or_comment_fails_closed(tmp_path, before, after):
    newline = b"\r\n" if b"\r\n" in before else b"\n"
    owned = (CLAUDE_START.encode() + newline + b"@AGENTS.md" +
             newline + CLAUDE_END.encode())
    inactive = before + owned + after
    with pytest.raises(ValueError, match="managed @AGENTS.md import is inactive"):
        managed_claude_rules(inactive)
    original = b"# Existing shared instructions\n"
    (tmp_path / "AGENTS.md").write_bytes(original)
    (tmp_path / "CLAUDE.md").write_bytes(inactive)
    with pytest.raises(ValueError, match="managed @AGENTS.md import is inactive"):
        asyncio.run(install_workspace(tmp_path))
    # Both user files and config outputs remain intact if validation fails.
    assert (tmp_path / "AGENTS.md").read_bytes() == original
    assert (tmp_path / "CLAUDE.md").read_bytes() == inactive
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".mcp.json").exists()


def test_inactive_managed_import_is_not_accepted_via_another_active_import():
    owned = (CLAUDE_START + "\n@AGENTS.md\n" + CLAUDE_END)
    existing = ("```md\n" + owned + "\n```\n@AGENTS.md\n").encode()
    with pytest.raises(ValueError, match="managed @AGENTS.md import is inactive"):
        managed_claude_rules(existing)


@pytest.mark.parametrize("unfinished", [b"```markdown\nExample\n", b"<!--\nExample\n"])
def test_installer_does_not_append_import_into_unclosed_markdown(tmp_path, unfinished):
    (tmp_path / "CLAUDE.md").write_bytes(unfinished)
    with pytest.raises(ValueError, match="managed @AGENTS.md import is inactive"):
        asyncio.run(install_workspace(tmp_path))
    assert (tmp_path / "CLAUDE.md").read_bytes() == unfinished
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".codex").exists()


@pytest.mark.parametrize("example", [
    b"- \x60\x60\x60md\n  @AGENTS.md\n  \x60\x60\x60\n",
    b"1. ~~~md\n   @AGENTS.md\n   ~~~\n",
    b"- - \x60\x60\x60md\n    @AGENTS.md\n    \x60\x60\x60\n",
    b"\x60\x60\n@AGENTS.md\n\x60\x60\n",
    b"Some text \x60\n@AGENTS.md\n\x60\n",
])
def test_claude_list_fences_and_multiline_spans_are_not_active_imports(example):
    result = managed_claude_rules(example)
    assert result.startswith(example)
    assert result.count(CLAUDE_START.encode()) == 1
    assert result.count(b"@AGENTS.md") == 2
    assert managed_claude_rules(result) == result


@pytest.mark.parametrize("before,after,error", [
    (b"\x60\nExample line\n", b"\n\x60\n",
     "managed @AGENTS.md import is inactive"),
    (b"\x60\x60\nExample line\n", b"\n\x60\x60\n",
     "managed @AGENTS.md import is inactive"),
    # The list prefix also violates the independent full-line marker invariant.
    (b"- \x60\x60\x60md\n  ", b"\n  \x60\x60\x60\n",
     "CLAUDE.md markers must occupy complete lines"),
])
def test_managed_claude_import_inside_nested_code_is_rejected(before, after, error):
    owned = (CLAUDE_START + "\n@AGENTS.md\n" + CLAUDE_END).encode()
    with pytest.raises(ValueError, match=error):
        managed_claude_rules(before + owned + after)


@pytest.mark.parametrize("unclosed", [
    b"\x60\nexample\n",
    b"\x60\x60\nexample\n",
])
def test_installer_ends_unclosed_inline_span_at_new_paragraph(tmp_path, unclosed):
    (tmp_path / "CLAUDE.md").write_bytes(unclosed)
    asyncio.run(install_workspace(tmp_path))
    installed = (tmp_path / "CLAUDE.md").read_bytes()
    assert installed.startswith(unclosed)
    assert installed.count(CLAUDE_START.encode()) == 1
    assert installed.count(b"@AGENTS.md") == 1
    # The blank separator starts a new Markdown paragraph. Reruns remain stable.
    asyncio.run(install_workspace(tmp_path))
    assert (tmp_path / "CLAUDE.md").read_bytes() == installed


def test_installer_rejects_unclosed_list_fence_without_partial_writes(tmp_path):
    unclosed = b"- \x60\x60\x60md\n  example\n"
    (tmp_path / "CLAUDE.md").write_bytes(unclosed)
    with pytest.raises(ValueError, match="managed @AGENTS.md import is inactive"):
        asyncio.run(install_workspace(tmp_path))
    assert (tmp_path / "CLAUDE.md").read_bytes() == unclosed
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".mcp.json").exists()


def test_closed_multiline_code_span_allows_new_managed_import():
    original = b"\x60\nnot an active import\n\x60\n"
    result = managed_claude_rules(original)
    assert result.startswith(original)
    assert result.count(CLAUDE_START.encode()) == 1
    assert managed_claude_rules(result) == result


@pytest.mark.parametrize("example", [
    b"\x60\x60\x60md\n    \x60\x60\x60\n@AGENTS.md\n\x60\x60\x60\n",
    b"~~~md\n    ~~~\n@AGENTS.md\n~~~\n",
    b"\x60\x60\x60md\n\t\x60\x60\x60\n@AGENTS.md\n\x60\x60\x60\n",
])
def test_four_space_fence_closer_cannot_expose_inactive_import(example):
    # The apparent closer is code content, so the import is an example.
    result = managed_claude_rules(example)
    assert result.startswith(example)
    assert result.count(b"@AGENTS.md") == 2
    assert result.count(CLAUDE_START.encode()) == 1
    assert managed_claude_rules(result) == result


def test_unclosed_fence_with_four_space_false_closer_fails_before_any_write(tmp_path):
    existing = b"\x60\x60\x60md\n    \x60\x60\x60\n@AGENTS.md\n"
    (tmp_path / "CLAUDE.md").write_bytes(existing)
    with pytest.raises(ValueError, match="managed @AGENTS.md import is inactive"):
        asyncio.run(install_workspace(tmp_path))
    assert (tmp_path / "CLAUDE.md").read_bytes() == existing
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".codex").exists()


@pytest.mark.parametrize("existing", [
    b"The token \x60<!--\x60 starts a comment.\n@AGENTS.md\n",
    b"\x60\x60<!--\x60\x60\n@AGENTS.md\n",
    b"Example \x60<!--\x60 text.\r\n@AGENTS.md\r\n",
])
def test_html_comment_delimiter_in_inline_code_does_not_hide_real_import(existing):
    assert managed_claude_rules(existing) == existing


def test_unclosed_html_block_after_unmatched_tick_needs_managed_import():
    # CommonMark starts an HTML block on the <!-- line; the apparent second
    # backtick does not close the previous paragraph's literal tick.
    existing = b"\x60\n<!--\n\x60\n@AGENTS.md\n"
    installed = managed_claude_rules(existing)
    assert installed.startswith(existing)
    assert installed.count(CLAUDE_START.encode()) == 1
    assert managed_claude_rules(installed) == installed


def test_inline_code_comment_example_still_allows_managed_import():
    original = b"Documentation: \x60<!--\x60 is a token, not a comment.\n"
    installed = managed_claude_rules(original)
    assert installed.startswith(original)
    assert installed.count(CLAUDE_START.encode()) == 1
    assert installed.count(b"@AGENTS.md") == 1
    assert managed_claude_rules(installed) == installed


@pytest.mark.parametrize("closer", [
    b"\x60\x60\x60",
    b" \x60\x60\x60",
    b"  \x60\x60\x60",
    b"   \x60\x60\x60",
])
def test_legal_top_level_fence_closers_allow_import(closer):
    existing = b"\x60\x60\x60md\n" + closer + b"\n@AGENTS.md\n"
    assert managed_claude_rules(existing) == existing


@pytest.mark.parametrize("existing", [
    b"\x60\n\n@AGENTS.md\n",
    b"\x60\x60\n  \n@AGENTS.md\n",
    b"\x60 before paragraph break\n\n@AGENTS.md\n",
])
def test_paragraph_break_ends_inline_code_before_real_import(existing):
    # Unmatched code ticks are not active across a blank paragraph separator.
    assert managed_claude_rules(existing) == existing


@pytest.mark.parametrize("example", [
    b"\x60\n\n<!--\n\x60\n@AGENTS.md\n-->\n",
    b"\x60\x60\n\n<!--\n\x60\x60\n@AGENTS.md\n-->\n",
    b"\x60\n  \n<!--\n\x60\n@AGENTS.md\n-->\n",
])
def test_paragraph_break_does_not_hide_subsequent_html_comment(example):
    # The import is a commented example. A fresh active managed block is needed.
    installed = managed_claude_rules(example)
    assert installed.startswith(example)
    assert installed.count(b"@AGENTS.md") == 2
    assert installed.count(CLAUDE_START.encode()) == 1
    assert managed_claude_rules(installed) == installed


def test_paragraph_break_does_not_clear_real_html_comment():
    example = b"<!--\n\n@AGENTS.md\n-->\n"
    installed = managed_claude_rules(example)
    assert installed.count(CLAUDE_START.encode()) == 1
    assert installed.startswith(example)


@pytest.mark.parametrize("list_start,fake_closer,real_closer", [
    (b"-\t\x60\x60\x60md\n", b"  \x60\x60\x60\n", b"    \x60\x60\x60\n"),
    (b"1.\t~~~md\n", b" \x7e\x7e\x7e\n", b"    ~~~\n"),
    (b"- \t\x60\x60\x60md\n", b"  \x60\x60\x60\n", b"    \x60\x60\x60\n"),
])
def test_tab_list_indent_cannot_close_fence_early(list_start, fake_closer, real_closer):
    # The outdented fake closer becomes a NEW top-level fence. Our
    # conservative checker must never accept its import as active.
    example = list_start + fake_closer + b"@AGENTS.md\n" + real_closer
    with pytest.raises(ValueError, match="managed @AGENTS.md import is inactive"):
        managed_claude_rules(example)


@pytest.mark.parametrize("list_start,closing", [
    (b"-\t\x60\x60\x60md\n", b"    \x60\x60\x60\n"),
    (b"1.\t~~~md\n", b"\t~~~\n"),
    (b"- \t\x60\x60\x60md\n", b"    \x60\x60\x60\n"),
])
def test_tab_list_indent_with_real_closer_allows_active_import(list_start, closing):
    existing = list_start + closing + b"@AGENTS.md\n"
    assert managed_claude_rules(existing) == existing


def test_tab_list_fence_inactive_marked_import_fails_without_writes(tmp_path):
    owned = (CLAUDE_START + "\n@AGENTS.md\n" + CLAUDE_END).encode()
    existing = b"-\t\x60\x60\x60md\n  \x60\x60\x60\n" + owned + b"\n    \x60\x60\x60\n"
    (tmp_path / "CLAUDE.md").write_bytes(existing)
    (tmp_path / "AGENTS.md").write_bytes(b"# Custom rules\n")
    with pytest.raises(ValueError, match="managed @AGENTS.md import is inactive"):
        asyncio.run(install_workspace(tmp_path))
    assert (tmp_path / "CLAUDE.md").read_bytes() == existing
    assert (tmp_path / "AGENTS.md").read_bytes() == b"# Custom rules\n"
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".mcp.json").exists()


@pytest.mark.parametrize("example", [
    b"- item\n\n  \x60\x60\x60md\n\x60\x60\x60\n@AGENTS.md\n\x60\x60\x60\n",
    b"1. item\n\n   ~~~md\n~~~\n@AGENTS.md\n~~~\n",
    b"- item\n\n  \x60\x60\x60md\n\x60\x60\x60\n@AGENTS.md\n\x60\x60\x60\n# end\n",
])
def test_list_continuation_fence_dedent_opens_new_top_level_fence(example):
    # Outdented delimiters form a new top-level fence, not a list closer.
    # If our independent scanners disagree on the subsequent context,
    # fail closed rather than write an import that might still be inactive.
    with pytest.raises(ValueError, match="managed @AGENTS.md import is inactive"):
        managed_claude_rules(example)


@pytest.mark.parametrize("existing", [
    b"- item\n\n  \x60\x60\x60md\n\x60\x60\x60\n@AGENTS.md\n",
    b"1. item\n\n   ~~~md\n~~~\n@AGENTS.md\n",
])
def test_unclosed_top_level_fence_after_list_dedent_fails_before_write(tmp_path, existing):
    (tmp_path / "CLAUDE.md").write_bytes(existing)
    (tmp_path / "AGENTS.md").write_bytes(b"# Existing rules\n")
    with pytest.raises(ValueError, match="managed @AGENTS.md import is inactive"):
        asyncio.run(install_workspace(tmp_path))
    assert (tmp_path / "CLAUDE.md").read_bytes() == existing
    assert (tmp_path / "AGENTS.md").read_bytes() == b"# Existing rules\n"
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".mcp.json").exists()


def test_list_continuation_fence_closed_inside_list_allows_real_import():
    existing = b"- item\n\n  \x60\x60\x60md\n  \x60\x60\x60\n@AGENTS.md\n"
    assert managed_claude_rules(existing) == existing


def test_lazy_list_continuation_is_not_trusted_as_standalone_import():
    existing = b"- item\n@AGENTS.md\n"
    installed = managed_claude_rules(existing)
    assert installed.startswith(existing)
    assert installed.count(CLAUDE_START.encode()) == 1
    assert installed.count(b"@AGENTS.md") == 2
    assert managed_claude_rules(installed) == installed


@pytest.mark.parametrize("existing", [
    b"@AGENTS.md\n---\n",
    b"@AGENTS.md\n===\n",
    b"@AGENTS.md\r\n---\r\n",
    b"@AGENTS.md\n   ---\n",
])
def test_setext_heading_is_not_accepted_as_active_claude_import(existing):
    # CommonMark renders these as top-level headings, not paragraphs,
    # despite their inline tokens also having level == 1.
    installed = managed_claude_rules(existing)
    assert installed.startswith(existing)
    assert installed.count(b"@AGENTS.md") == 2
    assert installed.count(CLAUDE_START.encode()) == 1
    assert managed_claude_rules(installed) == installed


def test_setext_heading_and_real_paragraph_import_are_distinct():
    existing = b"@AGENTS.md\n---\n\n@AGENTS.md\n"
    assert managed_claude_rules(existing) == existing


def test_setext_heading_inside_managed_claude_block_is_repaired(tmp_path):
    owned = (CLAUDE_START + "\n@AGENTS.md\n---\n" + CLAUDE_END + "\n").encode()
    (tmp_path / "AGENTS.md").write_bytes(b"# Existing lead rules\n")
    (tmp_path / "CLAUDE.md").write_bytes(owned)
    asyncio.run(install_workspace(tmp_path))
    repaired = (tmp_path / "CLAUDE.md").read_bytes()
    # Installer owns the entire marker block and replaces the Setext
    # heading with a genuine standalone paragraph import.
    assert repaired == (CLAUDE_START + "\n@AGENTS.md\n" + CLAUDE_END + "\n").encode()
    assert (tmp_path / "AGENTS.md").read_bytes().startswith(b"# Existing lead rules\n")
    asyncio.run(install_workspace(tmp_path))
    assert (tmp_path / "CLAUDE.md").read_bytes() == repaired


def test_claude_import_preserves_user_content_and_is_idempotent():
    original = b"# User Claude rules\r\nDo not modify.  \r\n"
    managed = managed_claude_rules(original)
    assert managed.startswith(original)
    assert managed.count(b"@AGENTS.md") == 1
    assert managed.count(CLAUDE_START.encode()) == 1
    assert managed.count(CLAUDE_END.encode()) == 1
    assert managed_claude_rules(managed) == managed
    assert managed_claude_rules(b"Other rules\n@AGENTS.md\n") == b"Other rules\n@AGENTS.md\n"
    assert managed_claude_rules(b"").count(b"@AGENTS.md") == 1


@pytest.mark.parametrize("broken", [
    CLAUDE_START.encode() + b"\n",
    CLAUDE_END.encode() + b"\n",
    (CLAUDE_END + "\n" + CLAUDE_START).encode(),
    (CLAUDE_START + "\n" + CLAUDE_START + "\n" + CLAUDE_END).encode(),
])
def test_malformed_claude_import_fails_before_any_workspace_changes(tmp_path, broken):
    (tmp_path / "AGENTS.md").write_bytes(b"# Existing Codex rules\n")
    (tmp_path / "CLAUDE.md").write_bytes(broken)
    with pytest.raises(ValueError, match="CLAUDE.md"):
        asyncio.run(install_workspace(tmp_path))
    assert (tmp_path / "AGENTS.md").read_bytes() == b"# Existing Codex rules\n"
    assert (tmp_path / "CLAUDE.md").read_bytes() == broken
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".codex").exists()


def test_claude_import_refuses_symlink(tmp_path):
    (tmp_path / "actual.md").write_text("keep")
    (tmp_path / "CLAUDE.md").symlink_to(tmp_path / "actual.md")
    with pytest.raises(ValueError, match="symlink"):
        asyncio.run(install_workspace(tmp_path))
    assert not (tmp_path / "AGENTS.md").exists()
