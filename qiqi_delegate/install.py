"""Project-scoped installer. Only its exact AGENTS.md markers may be managed."""
from __future__ import annotations

import json
import os
import re
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from pathlib import Path

START = "<!-- BEGIN HERDR-TASK-MCP RULES -->"
END = "<!-- END HERDR-TASK-MCP RULES -->"
MCP_START = "# >>> qiqi-delegate MCP (managed)"
MCP_END = "# <<< qiqi-delegate MCP (managed)"
SERVER = "qiqi_delegate"
DIRECT = "mcp__qiqi_delegate"

RULES = """## Quy tắc điều phối qua qiqi_delegate

- Lead không tự đọc, sửa, chạy lệnh, kiểm thử hoặc commit trong repository đích. Giao mọi công việc repository-scoped cho Peer qua MCP `qiqi_delegate`.
- Lead chịu trách nhiệm lập TaskPacket, chọn repository và route, quản lý dependency, review evidence và quyết định ACCEPT/RETRY/REPLAN/BLOCK.
- Chỉ sử dụng repository có tên chính xác trong `repos.yaml`. Không tự suy đoán đường dẫn, tạo, clone hoặc thêm repository nếu chưa được người dùng yêu cầu.
- Mỗi TaskPacket phải tự đủ nghĩa với `objective`, `scope`, `acceptance_criteria` và context hoặc constraints cần thiết. Không dựa vào lịch sử hội thoại mà Peer không thể truy cập.
- Khi giao việc khám phá, phân tích hoặc review, Lead đặt acceptance criteria yêu cầu giải thích luồng thực thi, giao diện/hợp đồng, trường hợp lỗi và evidence `file:line` phù hợp với phạm vi; không chỉ yêu cầu liệt kê công nghệ hoặc tệp.
- Trước ACCEPT, Lead phải đọc `agent_response` của đúng attempt qua `get_node_reviews`, đối chiếu từng acceptance criterion với bằng chứng. Nếu thiếu chi tiết, thiếu chứng cứ hoặc còn mâu thuẫn thì RETRY với feedback cụ thể; không ACCEPT chỉ vì Peer đã `settled`.
- Khi tổng hợp nhiều Peer, Lead giữ lại cơ chế hoạt động, chứng cứ nguồn và giới hạn xác minh quan trọng. Liên kết kết quả giữa các repository chỉ dựa trên evidence được Peer cung cấp; không đọc trực tiếp repository đích, không suy đoán contract chưa được kiểm chứng.
- Peer chỉ làm việc trong Git root và phạm vi được giao. Không đọc hoặc sửa repository khác; không tự điều phối Peer khác.
- Dùng TaskGraph để quản lý các task có dependency. Chỉ cho downstream chạy sau khi Lead ACCEPT upstream; chỉ chạy song song khi không xung đột phạm vi ghi.
- Dùng native captured response và evidence làm căn cứ review. Không đọc Herdr terminal để suy đoán final response; trạng thái `settled` không đồng nghĩa với ACCEPT.
- Khi Peer báo lỗi hoặc blocker, Lead xem evidence rồi quyết định RETRY, REPLAN hoặc BLOCK. Không retry vô hạn hoặc tự tiếp quản công việc của Peer.
- Không tự cài MCP, thêm rule hoặc sửa `AGENTS.md` trong repository đích. Quản lý cấu hình MCP và rule trong workspace điều phối.
- Không chạy Supervisor Broker. Lead chịu trách nhiệm review và quyết định cuối cùng.
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

def _validate_herdr_session(name: str | None) -> str | None:
    if name is None:
        return None
    if (not isinstance(name, str) or len(name) > 64
            or name in ("", ".", "..")
            or re.fullmatch(r"[A-Za-z0-9._-]+", name) is None):
        raise ValueError(
            "Herdr session must be 1–64 ASCII letters, digits, '.', '_' or '-' "
            "(excluding '.' and '..')"
        )
    return name


def _existing_herdr_session(codex: str, claude: str) -> str | None:
    """Preserve a project-scoped session choice when rerunning the installer."""
    old_codex = None
    if MCP_START in codex:
        _strip_own_section(codex)
        managed = codex.split(MCP_START, 1)[1].split(MCP_END, 1)[0]
        cfg = tomllib.loads(managed)
        old_codex = (cfg.get("mcp_servers", {})
                     .get(SERVER, {}).get("env", {}).get("QIQI_HERDR_SESSION"))
    old_claude = None
    if claude.strip():
        cfg = json.loads(claude)
        if isinstance(cfg, dict):
            old_claude = (cfg.get("mcpServers", {})
                          .get(SERVER, {}).get("env", {}).get("QIQI_HERDR_SESSION"))
    if old_codex and old_claude and old_codex != old_claude:
        raise ValueError("Conflicting Herdr sessions in project Codex/Claude MCP config")
    return _validate_herdr_session(old_codex or old_claude)


def codex_config(existing: str, python: Path, root: Path,
                 herdr_session: str | None = None) -> str:
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
        *(['QIQI_HERDR_SESSION = ' + json.dumps(herdr_session)]
          if herdr_session else []),
        MCP_END,
    ])
    return original.rstrip("\r\n") + ("\n\n" if original.strip() else "") + block + "\n"

def claude_config(existing: str, python: Path, root: Path,
                  herdr_session: str | None = None) -> str:
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
        "env": {
            "QIQI_WORKSPACE_ROOT": str(root),
            **({"QIQI_HERDR_SESSION": herdr_session} if herdr_session else {}),
        },
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

REPOS = """# Register existing Git roots relative to THIS workspace.
# Child repositories: path: frontend. Sibling repositories: path: ../frontend.
# Siblings must remain inside the workspace parent's directory.
# No qiqi_delegate code or MCP config is written into these Git repositories.
repositories: []
# Example siblings (when workspace is herdr-delegate-lab):
#   - name: frontend
#     path: ../frontend
#   - name: backend
#     path: ../backend
"""

async def install_workspace(root: Path, python: Path | None = None,
                            herdr_session: str | None = None) -> dict:
    root = root.resolve(strict=True)
    # sys.executable commonly points at <venv>/bin/python, itself a symlink
    # to a base interpreter (notably for uv-managed CPython). Resolving that
    # final symlink discards the venv's site-packages and breaks MCP startup.
    # Normalize to an absolute path WITHOUT dereferencing the final symlink.
    python = Path(os.path.abspath(os.fspath(python or sys.executable)))
    if not python.is_file():
        raise ValueError(f"Python interpreter does not exist: {python}")
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
    herdr_session = (
        _validate_herdr_session(herdr_session)
        if herdr_session is not None
        else _existing_herdr_session(c_old, m_old)
    )
    a_new = managed_rules(a_old)
    c_new = codex_config(c_old, python, root, herdr_session=herdr_session)
    m_new = claude_config(m_old, python, root, herdr_session=herdr_session)
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
            "agents_marker": START, "python": str(python),
            "herdr_session": herdr_session}

def main():
    import argparse
    import asyncio
    parser = argparse.ArgumentParser(description="Install QiQi MCP only in one workspace")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--herdr-session",
        help="Target an existing named Herdr session in project MCP configs",
    )
    args = parser.parse_args()
    print(json.dumps(
        asyncio.run(install_workspace(args.workspace, herdr_session=args.herdr_session)),
        ensure_ascii=False, indent=2
    ))

if __name__ == "__main__":
    main()
