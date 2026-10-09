"""Project-scoped installer. Only its exact AGENTS.md markers may be managed."""
from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from pathlib import Path

START = "<!-- BEGIN HERDR-TASK-MCP RULES -->"
END = "<!-- END HERDR-TASK-MCP RULES -->"
MCP_START = "# >>> qiqi-delegate MCP (managed)"
MCP_END = "# <<< qiqi-delegate MCP (managed)"
SERVER = "qiqi_delegate"
DIRECT = "mcp__qiqi_delegate"

RULES = """## QiQi Delegate — managed rules

- Khi cần giao việc, dùng MCP qiqi_delegate tại workspace này.
- Lead giữ quyền chọn repository, route, dependency và quyết định ACCEPT/RETRY/REPLAN/BLOCK.
- Mỗi TaskPacket phải có objective, scope và acceptance_criteria đủ nghĩa.
- Repository là name trong repos.yaml. Không yêu cầu Peer đọc repository anh em.
- Không suy đoán kết quả từ Herdr terminal. Dùng native captured agent_response.
- Một Peer settled chưa đồng nghĩa được ACCEPT. Downstream chỉ chạy sau ACCEPT.
- Không sửa file bên ngoài Git root của Peer, trừ input được cấp quyền rõ ràng.
- Không chạy Supervisor Broker. Không yêu cầu cài module trong repository con.
"""

def managed_rules(existing: bytes) -> bytes:
    """Preserve every byte outside the managed markers, including CRLF and trailing spaces."""
    text = existing.decode("utf-8")
    starts, ends = text.count(START), text.count(END)
    if (starts, ends) not in ((0, 0), (1, 1)):
        raise ValueError("AGENTS.md has malformed or duplicate qiqi_delegate markers; no changes written")
    newline = "\r\n" if "\r\n" in text else "\n"
    block = START + newline + RULES.rstrip("\n").replace("\n", newline) + newline + END
    if starts == 1:
        a, b = text.index(START), text.index(END)
        if b < a:
            raise ValueError("AGENTS.md marker ordering is invalid")
        # A managed block must start and end on their own full lines.
        if (a and text[a - 1] not in "\r\n") or (
            b + len(END) < len(text) and text[b + len(END)] not in "\r\n"
        ):
            raise ValueError("AGENTS.md markers must occupy complete lines")
        result = text[:a] + block + text[b + len(END):]
    else:
        suffix = "" if not text else ("" if text.endswith(("\n", "\r")) else newline)
        result = text + suffix + (newline if text else "") + block + newline
    return result.encode("utf-8")

def _strip_own_section(text: str) -> str:
    count_start, count_end = text.count(MCP_START), text.count(MCP_END)
    if (count_start, count_end) not in ((0, 0), (1, 1)):
        raise ValueError("malformed qiqi MCP managed section in .codex/config.toml")
    if count_start == 0:
        return text
    i, j = text.index(MCP_START), text.index(MCP_END)
    if j < i:
        raise ValueError("invalid managed section ordering")
    # Keep unrelated content exactly. The following newline belongs to the block.
    end = j + len(MCP_END)
    if text[end:end + 2] == "\r\n":
        end += 2
    elif text[end:end + 1] == "\n":
        end += 1
    return text[:i] + text[end:]

def codex_config(existing: str, python: Path, root: Path) -> str:
    original = _strip_own_section(existing)
    try:
        parsed = tomllib.loads(original)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(".codex/config.toml has invalid TOML; refusing to overwrite") from exc
    servers = parsed.get("mcp_servers", {})
    if isinstance(servers, dict) and SERVER in servers:
        raise ValueError("qiqi_delegate server is defined outside managed block")
    has_feature = isinstance(parsed.get("features"), dict) and (
        "code_mode" in parsed["features"]
    )
    feature_lines = []
    if not has_feature:
        feature_lines = ["[features.code_mode]", 'direct_only_tool_namespaces = ["' + DIRECT + '"]', ""]
    else:
        code_mode = parsed["features"]["code_mode"]
        if not isinstance(code_mode, dict):
            raise ValueError("features.code_mode must be a table")
        current = code_mode.get("direct_only_tool_namespaces")
        if current is not None and (
            not isinstance(current, list) or
            not all(isinstance(v, str) for v in current)
        ):
            raise ValueError("direct_only_tool_namespaces must be an array of strings")
        if current is None or DIRECT not in current:
            matches = list(re.finditer(r"(?m)^[ \t]*\[features\.code_mode\][ \t]*(?:#.*)?$", original))
            if len(matches) != 1:
                raise ValueError("unable to locate unique [features.code_mode] table")
            head = matches[0]
            tail = re.search(r"(?m)^[ \t]*\[\[?[^\n]+", original[head.end():])
            end = head.end() + tail.start() if tail else len(original)
            body = original[head.end():end]
            if current is None:
                original = original[:head.end()] + '\ndirect_only_tool_namespaces = ["' + DIRECT + '"]' + original[head.end():]
            else:
                assignment = re.search(r"(?s)(^[ \t]*direct_only_tool_namespaces[ \t]*=[ \t]*)(\[[^]]*\])",
                                       body, re.MULTILINE)
                if not assignment:
                    raise ValueError("cannot safely merge direct_only_tool_namespaces")
                replacement = json.dumps([*current, DIRECT], ensure_ascii=False)
                start = head.end() + assignment.start(2)
                stop = head.end() + assignment.end(2)
                original = original[:start] + replacement + original[stop:]

    block = "\n".join([
        MCP_START, *feature_lines,
        "[mcp_servers.qiqi_delegate]",
        'command = ' + json.dumps(str(python)),
        'args = ["-m", "qiqi_delegate.server"]',
        "[mcp_servers.qiqi_delegate.env]",
        'QIQI_WORKSPACE_ROOT = ' + json.dumps(str(root)),
        MCP_END,
    ])
    return original.rstrip("\r\n") + ("\n\n" if original.strip() else "") + block + "\n"

def claude_config(existing: str, python: Path, root: Path) -> str:
    try:
        config = json.loads(existing) if existing.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(".mcp.json contains invalid JSON") from exc
    if not isinstance(config, dict):
        raise ValueError(".mcp.json must be an object")
    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(".mcp.json mcpServers must be an object")
    if SERVER in servers:
        old = servers[SERVER]
        if not isinstance(old, dict) or old.get("args") != ["-m", "qiqi_delegate.server"]:
            raise ValueError("qiqi_delegate exists as an unmanaged Claude MCP; refusing overwrite")
    servers[SERVER] = {
        "command": str(python),
        "args": ["-m", "qiqi_delegate.server"],
        "env": {"QIQI_WORKSPACE_ROOT": str(root)},
    }
    config["mcpServers"] = servers
    return json.dumps(config, ensure_ascii=False, indent=2) + "\n"

ROUTES = """# Routes and arguments belong to this workspace, not any child repository.
routes:
  codex-balanced:
    agent: codex
    args: []
  claude-balanced:
    agent: claude
    args: []
# Example opt-in:
#   codex: args: ["--yolo"]
#   claude: args: ["--permission-mode", "auto"]
"""

REPOS = """# Register existing Git roots relative to this workspace.
# No qiqi_delegate code or MCP config is written into these repositories.
repositories: []
# Example entries:
#   - name: frontend
#     path: frontend
#   - name: backend
#     path: backend
"""

async def install_workspace(root: Path, python: Path | None = None) -> dict:
    root = root.resolve(strict=True)
    python = (python or Path(sys.executable)).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace root must be a directory")
    agents = root / "AGENTS.md"
    codex = root / ".codex" / "config.toml"
    claude = root / ".mcp.json"
    for path in (agents, codex, claude, root / ".codex",
                 root / ".herdr-task-mcp"):
        if path.is_symlink():
            raise ValueError(f"refusing symlink: {path}")
    a_old = agents.read_bytes() if agents.exists() else b""
    c_old = codex.read_text(encoding="utf-8") if codex.exists() else ""
    m_old = claude.read_text(encoding="utf-8") if claude.exists() else ""
    # Complete all validations before modifying any file.
    a_new = managed_rules(a_old)
    c_new = codex_config(c_old, python, root)
    m_new = claude_config(m_old, python, root)
    (root / ".codex").mkdir(exist_ok=True)
    state = root / ".herdr-task-mcp"
    state.mkdir(mode=0o700, exist_ok=True)
    (state / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    agents.write_bytes(a_new)
    codex.write_text(c_new, encoding="utf-8")
    claude.write_text(m_new, encoding="utf-8")
    for name, value in (("repos.yaml", REPOS), ("agent-routing.yaml", ROUTES)):
        path = root / name
        if not path.exists():
            path.write_text(value, encoding="utf-8")
    return {"workspace": str(root), "mcp": SERVER,
            "agents_marker": START, "python": str(python)}

def main():
    import argparse
    import asyncio
    parser = argparse.ArgumentParser(description="Install QiQi MCP only in one workspace")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(asyncio.run(install_workspace(args.workspace)), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
