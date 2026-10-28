"""Project-scoped installer. Only its exact AGENTS.md markers may be managed."""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import sys
import tempfile
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from pathlib import Path

from markdown_it import MarkdownIt


START = "<!-- BEGIN HERDR-TASK-MCP RULES -->"
END = "<!-- END HERDR-TASK-MCP RULES -->"
CLAUDE_START = "<!-- BEGIN HERDR-TASK-MCP CLAUDE IMPORT -->"
CLAUDE_END = "<!-- END HERDR-TASK-MCP CLAUDE IMPORT -->"
MCP_START = "# >>> qiqi-delegate MCP (managed)"
MCP_END = "# <<< qiqi-delegate MCP (managed)"
SERVER = "qiqi_delegate"
DIRECT = "mcp__qiqi_delegate"

RULES = """## Quy tắc điều phối qua qiqi_delegate

- Lead không tự đọc, sửa, chạy lệnh, kiểm thử hoặc commit trong repository đích. Giao mọi công việc repository-scoped cho Peer qua MCP `qiqi_delegate`.
- Lead chịu trách nhiệm lập TaskPacket, chọn repository và route, quản lý dependency, review evidence và quyết định ACCEPT/RETRY/REPLAN/BLOCK.
- Với tác vụ mới, ưu tiên submit_workflow để đăng ký user request, source, readiness assessment và single_task/graph trong một call; API cũ prepare_task_request/submit_context_assessment giữ cho phục hồi và tương thích. Tài liệu/handoff KHÔNG bắt buộc.
- Dùng source alias (alias:<tên>) trong submit_workflow; server thay bằng source ID thực và giữ nguyên evidence/provenance.
- Sau Discovery, dùng continue_workflow(request_id, expected_revision, assessment, single_task/graph) để reassess và giao việc trên cùng Task Request. Dùng source IDs do Discovery capture trong evidence_refs; không tạo lại request và không bỏ provenance.
- Khi direct, submit_workflow tự dispatch wave đầu; get_review_bundle mặc định dùng ngân sách TỔNG 32000 ký tự JSON (max_total_chars; cho phép 1024..64000), tối đa 8 review/trang. Server tự phân bổ evidence: một Peer response 12000 ký tự có thể nhận đủ trong một call nếu vừa ngân sách. max_chars_per_review là trần tùy chọn 1..64000 cho mỗi review, KHÔNG phải tổng ngân sách; không tự áp trần 8000 hoặc tăng vô hạn.
- Nếu get_review_bundle trả response_truncated=true, đọc đúng node_id, attempt_id và next_offset bằng get_review_evidence(max_chars 1..24000), lặp theo next_offset đến null trước khi quyết định ACCEPT. Nếu next_review_offset khác null, lấy trang sau với expected_revision đúng bằng revision trang đầu để tránh bỏ sót evidence khi graph thay đổi. Không ACCEPT chỉ vì preview cho thấy settled.
- Nếu submit_workflow, continue_workflow hoặc submit_decisions(auto_continue=true) trả review_bundle_error và review_bundle.incomplete=true, graph và Peer/Lead decision ĐÃ được lưu; KHÔNG submit lại workflow hoặc quyết định vì sẽ gây duplicate. Dùng graph_run_id/revision/review_locators trong fallback để đọc đúng attempt bằng get_review_evidence hoặc gọi get_review_bundle(max_total_chars=64000) khi metadata vừa. Bản thân get_review_bundle vẫn fail-closed nếu một review có metadata vượt ngân sách tối đa.
- Dùng submit_decisions(auto_continue=true) để server tự dispatch các runnable wave an toàn sau quyết định; không bỏ semantic review, ACCEPT checkpoint, claim hoặc revision guard.
- Chỉ gọi Discovery khi thiếu thông tin ảnh hưởng đến kế hoạch. Nếu đủ rõ thì chọn readiness `direct`; nếu thiếu một fact thì `targeted_discovery`; nếu chưa hiểu hệ thống thì `full_discovery`; nếu ý định user mơ hồ thì báo blocker hoặc hỏi lại.
- Với workflow mới, chỉ tạo graph khi assessment `direct` và ánh xạ node với requirement IDs; với workflow cũ không có task_request_id phải coi là legacy_unassessed, không tuyên bố đã qua gate.
- Discovery được phép điều tra nhiều Git root đã đăng ký thông qua delegate_discovery/--add-dir; đây là ngoại lệ CHỈ để đọc. Cấm sửa/tạo/xóa file, commit, chạy command gây side effect. Với --yolo, cấm ghi bằng prompt KHÔNG phải sandbox.
- Nguồn được nhắc đến (spec, handoff, inline, Peer evidence) là optional. Tách user instruction, claim từ tài liệu, evidence do Peer báo cáo và unknown. Không tự coi label source là xác minh nội dung.
- Chỉ sử dụng repository có tên chính xác trong `repos.yaml`. Không tự suy đoán đường dẫn, tạo, clone hoặc thêm repository nếu chưa được người dùng yêu cầu.
- Mỗi TaskPacket phải tự đủ nghĩa với `objective`, `scope`, `acceptance_criteria` và context hoặc constraints cần thiết. Không dựa vào lịch sử hội thoại mà Peer không thể truy cập.
- Lead dùng chung một schema `TaskPacket` cho analysis, coding, testing và documentation. Chọn `acceptance_criteria` và `constraints` theo mục tiêu thực tế; không cần schema hay node kind riêng cho từng loại.
- Với analysis/discovery/review: chỉ chọn các tiêu chí áp dụng cho yêu cầu, như luồng thực thi, contract, nhánh lỗi, giới hạn xác minh và evidence `file:line` khi liên quan; task tra cứu đơn giản chỉ cần câu trả lời với bằng chứng trực tiếp, không buộc phân tích toàn bộ hệ thống. Nếu task chỉ đọc, ghi rõ không sửa file trong `constraints`.
- Với coding: nêu hành vi cần triển khai, phạm vi thay đổi, contract phải giữ và tiêu chí kiểm thử liên quan; Peer báo cáo file thay đổi, test đã chạy và phần chưa kiểm chứng.
- Với testing: xác định hành vi/test cases cần kiểm tra, việc tạo hoặc sửa test có được phép hay không, lệnh kiểm thử phù hợp và cách báo cáo pass/fail, lỗi, test chưa chạy; không suy diễn pass khi chưa thực thi.
- Với documentation: yêu cầu nội dung khớp implementation/contract đã xác minh, ví dụ đúng thực tế và các thay đổi tài liệu cụ thể; không bịa đặt tính năng hay kết quả kiểm thử.
- Với task kết hợp nhiều loại việc, ghép các tiêu chí liên quan; không áp checklist không liên quan hoặc báo cáo dài cho nhiệm vụ đơn giản.
- Trước khi giao coding, Lead chọn `SINGLE_TASK` (một Peer) hoặc `MULTI_TASK` (nhiều task). Readiness `direct` chỉ xác nhận đủ điều kiện giao việc, không quyết định số task.
- Với coding, bắt buộc `MULTI_TASK` nếu có ít nhất hai hành vi có thể implement và ACCEPT riêng, hoặc phải ACCEPT upstream trước khi làm downstream.
- Với coding, cũng chọn `MULTI_TASK` nếu các phần có thể retry riêng và retry chung gây lặp đáng kể công việc.
- Với coding, chọn `SINGLE_TASK` khi chỉ có một kết quả chức năng, các thay đổi gắn chặt và không cần checkpoint. Không chia theo module, layer hoặc file. Giữ code và test liên quan cùng task.
- Với analysis, chỉ chia câu hỏi hoặc giả thuyết độc lập với evidence riêng. Giữ một Peer để truy vết một flow trong cùng Git root.
- Với Discovery, ưu tiên một `delegate_discovery` khi có blocker cần điều tra xuyên Git root. Chỉ tách các câu hỏi độc lập. Đánh giá lại readiness sau mỗi lượt; không gọi Discovery khi assessment `direct`.
- Mọi `repo_task` chỉ làm việc trong một Git root. Task ghi nhiều Git root (kể cả testing/documentation-only) phải giao riêng theo từng root. Với analysis xuyên root đã `direct`, giao task từng root rồi Lead tổng hợp evidence; chỉ dùng Discovery nếu có blocker và assessment phù hợp.
- Khi chia task, Lead nêu `objective`, `scope`, `acceptance_criteria` riêng. Chỉ khai báo `depends_on` cho dependency thực. Review từng node và REPLAN khi phát hiện dependency mới.
- Không bắt buộc chia theo kết quả chức năng cho review, testing-only hoặc documentation-only. Quy tắc một Git root vẫn áp dụng.
- Trước ACCEPT, Lead phải đọc `agent_response` của đúng attempt qua `get_review_bundle` và `get_review_evidence` nếu response bị cắt (hoặc qua `get_node_reviews` legacy), đối chiếu từng acceptance criterion với bằng chứng. Nếu thiếu chi tiết, thiếu chứng cứ hoặc còn mâu thuẫn thì RETRY với feedback cụ thể; không ACCEPT chỉ vì Peer đã `settled`.
- Khi tổng hợp nhiều Peer, Lead giữ lại cơ chế hoạt động, chứng cứ nguồn và giới hạn xác minh quan trọng. Liên kết kết quả giữa các repository chỉ dựa trên evidence được Peer cung cấp; không đọc trực tiếp repository đích, không suy đoán contract chưa được kiểm chứng.
- Implementation Peer chỉ làm việc trong Git root và phạm vi được giao. Discovery Peer chỉ được đọc những Git root được cấp rõ ràng trong --add-dir; không sửa bất kỳ repo nào. Không tự điều phối Peer khác.
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

def _conservative_claude_import_lines(text: str) -> set[int]:
    """Find standalone imports outside Markdown code and HTML comments.

    Record exact line offsets: another active import cannot validate an
    inactive installer-owned block. This conservative scanner is not a full
    CommonMark renderer; uncertain contexts must not authorize imports.
    """
    active: set[int] = set()
    fence_char: str | None = None
    fence_width = 0
    fence_base_indent = 0
    inline_ticks: int | None = None
    comment_depth = 0
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        start = offset
        offset += len(raw_line)
        line = raw_line.rstrip("\r\n")
        if fence_char is not None:
            # CommonMark closers allow at most three columns of indentation
            # relative to the container. A tab can advance to the next
            # 4-column stop; it is not equivalent to a single space.
            closing = re.fullmatch(
                r"([ \t]*)" + re.escape(fence_char)
                + "{" + str(fence_width) + r",}[ \t]*", line
            )
            if closing:
                columns = len(closing.group(1).expandtabs(4))
                if fence_base_indent <= columns <= fence_base_indent + 3:
                    fence_char = None
            continue

        # Blank lines end Markdown paragraphs and unmatched inline-code spans.
        # HTML comments, unlike inline spans, can continue past blank lines.
        if not line.strip():
            inline_ticks = None

        if inline_ticks is None and not comment_depth:
            candidate = line
            list_prefix_length = 0
            while True:
                item = re.match(
                    r" {0,3}(?:[-+*]|\d{1,9}[.)])[ \t]+", candidate
                )
                if not item:
                    break
                list_prefix_length += item.end()
                candidate = candidate[item.end():]
            opener = re.match(r" {0,3}(\x60{3,}|~{3,})", candidate)
            if opener:
                fence_char = opener.group(1)[0]
                fence_width = len(opener.group(1))
                # List marker indentation is measured in *columns*, not
                # characters. E.g. "-\\t" starts its content at column 4.
                fence_base_indent = len(line[:list_prefix_length].expandtabs(4))
                continue

        if (
            inline_ticks is None and not comment_depth
            and re.fullmatch(r" {0,3}@AGENTS\.md[ \t]*", line)
        ):
            active.add(start)

        # Process delimiters in source order: an HTML-comment opener inside
        # inline code must not start a comment spanning subsequent lines.
        i = 0
        while i < len(line):
            if comment_depth:
                if line.startswith("<!--", i):
                    comment_depth += 1
                    i += 4
                elif line.startswith("-->", i):
                    comment_depth -= 1
                    i += 3
                else:
                    i += 1
            elif inline_ticks is None and line[i] == "\\" and i + 1 < len(line):
                # Markdown escapes apply outside code spans only.
                i += 2
            elif line[i] == "\x60":
                end = i + 1
                while end < len(line) and line[end] == "\x60":
                    end += 1
                width = end - i
                if inline_ticks is None:
                    inline_ticks = width
                elif inline_ticks == width:
                    inline_ticks = None
                i = end
            elif inline_ticks is None and line.startswith("<!--", i):
                comment_depth = 1
                i += 4
            else:
                i += 1
    return active


def _active_claude_import_lines(text: str) -> set[int]:
    """Verify candidate imports against CommonMark block/inline parsing.

    The conservative lexical scanner rejects ambiguous nested HTML comments
    and unmatched code spans. The CommonMark parser independently rejects
    fenced/code-block imports, including continuation lines of list items
    whose containers end before a new top-level fence begins.
    """
    candidates = _conservative_claude_import_lines(text)
    if not candidates:
        return set()
    lines = text.splitlines(keepends=True)
    starts = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    confirmed = set()
    tokens = MarkdownIt("commonmark").parse(text)
    for index, token in enumerate(tokens):
        # A top-level Setext heading also has an inline token at level 1.
        # Only a real top-level paragraph may contain an active import.
        # Requiring its immediate paragraph_open parent excludes headings,
        # list continuations, HTML blocks and other Markdown containers.
        if (
            token.type != "inline"
            or token.level != 1
            or not token.map
            or index == 0
            or tokens[index - 1].type != "paragraph_open"
            or tokens[index - 1].level != 0
        ):
            continue
        # Require actual text, not code_inline/html_inline/image content.
        if not any(
            child.type == "text" and "@AGENTS.md" in child.content
            for child in (token.children or [])
        ):
            continue
        first, last = token.map
        for index in range(first, min(last, len(lines))):
            if starts[index] in candidates:
                confirmed.add(starts[index])
    return confirmed


def _has_active_claude_import(text: str) -> bool:
    """True only for an import that Claude can actually use."""
    return bool(_active_claude_import_lines(text))


def managed_claude_rules(existing: bytes) -> bytes:
    """Import the shared AGENTS.md into Claude while preserving user content."""
    text = existing.decode("utf-8")
    starts, ends = text.count(CLAUDE_START), text.count(CLAUDE_END)
    if (starts, ends) not in ((0, 0), (1, 1)):
        raise ValueError("CLAUDE.md has malformed or duplicate qiqi_delegate markers; no changes written")
    newline = "\r\n" if "\r\n" in text else "\n"
    block = CLAUDE_START + newline + "@AGENTS.md" + newline + CLAUDE_END
    if starts:
        a, b = text.index(CLAUDE_START), text.index(CLAUDE_END)
        if b < a:
            raise ValueError("CLAUDE.md marker ordering is invalid")
        if (a and text[a - 1] not in "\r\n") or (
            b + len(CLAUDE_END) < len(text)
            and text[b + len(CLAUDE_END)] not in "\r\n"
        ):
            raise ValueError("CLAUDE.md markers must occupy complete lines")
        result = text[:a] + block + text[b + len(CLAUDE_END):]
    else:
        # Preserve an existing active import, but not a code/example occurrence.
        if _has_active_claude_import(text):
            return existing
        suffix = "" if not text else ("" if text.endswith(("\n", "\r")) else newline)
        result = text + suffix + (newline if text else "") + block + newline
    # Check the exact owned import, not just any other active import in the file.
    import_start = result.index(CLAUDE_START) + len(CLAUDE_START) + len(newline)
    if import_start not in _active_claude_import_lines(result):
        raise ValueError(
            "CLAUDE.md managed @AGENTS.md import is inactive inside a "
            "Markdown code span/fence or HTML comment; move the managed block outside "
            "that context before reinstalling"
        )
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

ROUTES = """# One Claude route per model and effort. Pick the lowest sufficient effort.
# All Claude routes default to --permission-mode auto; this is not a read-only sandbox.
routes:
  claude-low:
    agent: claude
    args: ["--model", "claude-sonnet-5-5", "--effort", "low", "--permission-mode", "auto"]
    description: "Tra cứu nhanh, tìm file/symbol, xác minh dữ kiện nhỏ; không cần suy luận nhiều bước."

  claude-balanced:
    agent: claude
    args: ["--model", "claude-sonnet-5-5", "--effort", "medium", "--permission-mode", "auto"]
    description: "Mặc định cho coding, bugfix, tài liệu, test đơn giản khi phạm vi và tiêu chí rõ ràng."

  claude-high:
    agent: claude
    args: ["--model", "claude-sonnet-5-5", "--effort", "high", "--permission-mode", "auto"]
    description: "Phân tích, planning, discovery có trọng tâm, testing nhiều nhánh."

  claude-xhigh:
    agent: claude
    args: ["--model", "claude-sonnet-5-5", "--effort", "xhigh", "--permission-mode", "auto"]
    description: "Full discovery, debug khó, refactor lớn, security review."

  claude-max:
    agent: claude
    args: ["--model", "claude-sonnet-5-5", "--effort", "max", "--permission-mode", "auto"]
    description: "Chỉ chọn khi vấn đề đặc biệt khó hoặc rủi ro cao mà high/xhigh chưa đủ; tốn nhiều tài nguyên."

  # Legacy Codex routes retain distinct CLI permission profiles.
  codex-balanced:
    agent: codex
    args: ["--yolo"]
    description: "Tác vụ Codex với --yolo, chỉ dùng với repository đáng tin cậy."

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

def _stage_install_file(directory: Path, content: bytes, mode: int | None = None) -> Path:
    """Stage within the destination parent, retaining its creation semantics.

    A new file uses O_CREAT with mode 0666 so the actual umask, setgid parent
    and default ACL govern ownership and permissions as with a direct write.
    Existing files instead receive their original metadata before commit.
    """
    if mode is None:
        for _ in range(10):
            staged = directory / (".qiqi-install-" + secrets.token_hex(16))
            try:
                fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError(f"unable to allocate staging file in {directory}")
    else:
        fd, name = tempfile.mkstemp(prefix=".qiqi-install-", dir=directory)
        staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(staged, mode)
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _stage_install_rollback(original: Path) -> Path:
    """Hard-link the original inode inside its protected destination parent.

    Placing backups in the workspace root could expose readable files from
    restricted subdirectories. A direct link with an unpredictable name also
    avoids the unlink-then-link gap of a temporary placeholder.
    """
    for _ in range(10):
        backup = original.parent / (".qiqi-install-" + secrets.token_hex(16))
        try:
            os.link(original, backup)
            return backup
        except FileExistsError:
            continue
    raise FileExistsError(f"unable to allocate rollback link in {original.parent}")


def _preserve_install_metadata(original: Path, staged: Path) -> None:
    """Clone and verify metadata before any target file can be replaced.

    POSIX ACLs on Linux are xattrs. Refuse files on platforms where we cannot
    inspect/copy them rather than silently changing their security metadata.
    """
    required = ("listxattr", "getxattr", "setxattr")
    if not all(hasattr(os, name) for name in required):
        raise ValueError(f"cannot safely preserve file xattrs: {original}")
    source = original.stat()
    target = staged.stat()
    if (target.st_uid, target.st_gid) != (source.st_uid, source.st_gid):
        os.chown(staged, source.st_uid, source.st_gid)
    os.chmod(staged, stat.S_IMODE(source.st_mode))
    # Explicitly copy all visible xattrs, including POSIX ACL/security attrs,
    # then verify no source attributes were dropped or unexpected ones added.
    source_attrs = set(os.listxattr(original))
    for name in source_attrs:
        os.setxattr(staged, name, os.getxattr(original, name))
    if set(os.listxattr(staged)) != source_attrs:
        raise ValueError(f"cannot safely reproduce file xattrs: {original}")
    for name in source_attrs:
        if os.getxattr(staged, name) != os.getxattr(original, name):
            raise ValueError(f"cannot safely reproduce file xattr {name}: {original}")
    verified = staged.stat()
    if (
        (verified.st_uid, verified.st_gid) != (source.st_uid, source.st_gid)
        or stat.S_IMODE(verified.st_mode) != stat.S_IMODE(source.st_mode)
    ):
        raise ValueError(f"cannot safely reproduce file ownership/mode: {original}")


def _commit_workspace_files(root: Path, files: dict[Path, bytes]) -> None:
    """Stage everything before writes; restore all earlier files on failure.

    Temporary files and rollback hard links live in their respective
    destination directories (including protected subdirectories), permitting
    same-filesystem atomic os.replace without weakening parent access rules.
    Rollback hard links retain the original inodes and security metadata;
    staged replacements must reproduce that metadata before commit. This
    protects against ordinary I/O exceptions, not crashes or concurrent writes.
    """
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    replaced: list[Path] = []
    created_dirs: list[Path] = []
    failed_restores: set[Path] = set()
    try:
        # Create parents before staging so all new files inherit the metadata
        # of their actual destination directory, not the workspace root.
        # No pre-existing file is replaced until all staging is successful.
        for directory, mode in (
            (root / ".codex", None),
            (root / ".herdr-task-mcp", 0o700),
        ):
            if directory.is_symlink():
                raise ValueError(f"refusing symlink: {directory}")
            if not directory.exists():
                directory.mkdir(mode=mode or 0o777)
                created_dirs.append(directory)
            elif not directory.is_dir():
                raise ValueError(f"expected install directory: {directory}")

        for destination, data in files.items():
            # exists() follows symlinks and returns False for dangling ones.
            # Reject all managed symlinks before inspecting or linking inodes.
            if destination.is_symlink():
                raise ValueError(f"refusing symlink: {destination}")
            if destination.exists():
                if not destination.is_file():
                    raise ValueError(f"expected regular install file: {destination}")
                old_mode = stat.S_IMODE(destination.stat().st_mode)
                # os.replace may bypass a file's read-only flag on POSIX if
                # its parent directory is writable; honor user intent.
                if not old_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                    raise PermissionError(f"refusing read-only install file: {destination}")
                old_data = destination.read_bytes()
                if old_data == data:
                    continue
                # A hard-linked backup preserves the original inode's entire
                # metadata on rollback (including ownership and ACLs).
                backups[destination] = _stage_install_rollback(destination)
            else:
                old_mode = None
            staged[destination] = _stage_install_file(destination.parent, data, old_mode)
            if destination in backups:
                _preserve_install_metadata(destination, staged[destination])

        for destination, replacement in staged.items():
            os.replace(replacement, destination)
            replaced.append(destination)
    except BaseException as error:
        restore_errors = []
        for destination in reversed(replaced):
            try:
                if destination in backups:
                    os.replace(backups[destination], destination)
                else:
                    destination.unlink()
            except OSError as restore_error:
                failed_restores.add(destination)
                restore_errors.append(f"{destination}: {restore_error}")
        # Staged files now live inside their destination parents. Remove
        # uncommitted stages before attempting to remove newly made parents.
        for temporary in staged.values():
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                restore_errors.append(f"{temporary}: {cleanup_error}")
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError as restore_error:
                restore_errors.append(f"{directory}: {restore_error}")
        if restore_errors:
            raise RuntimeError(
                "workspace install failed and rollback was incomplete: "
                + "; ".join(restore_errors)
            ) from error
        raise
    finally:
        for temporary in (*staged.values(), *(
            backup for destination, backup in backups.items()
            if destination not in failed_restores
        )):
            temporary.unlink(missing_ok=True)


def migrate_legacy_evaluator_routes(existing: str) -> tuple[str, list[str], list[str]]:
    """Migrate exact historical installer defaults, preserving custom YAML routes.

    The legacy YAML is user-editable. Match both the parsed route and its exact
    installer-authored stanza before removal, and never serialize other routes.
    """
    import yaml
    from yaml.nodes import MappingNode, ScalarNode

    try:
        document = yaml.safe_load(existing)
        root_node = yaml.compose(existing)
    except yaml.YAMLError as exc:
        raise ValueError("agent-routing.yaml has invalid YAML; refusing migration") from exc
    if not isinstance(document, dict) or not isinstance(document.get("routes"), dict):
        return existing, [], []

    def entries(node: MappingNode, scope: str) -> dict[str, tuple]:
        found: dict[str, tuple] = {}
        for key, value in node.value:
            if not isinstance(key, ScalarNode):
                continue
            if key.value in found:
                raise ValueError(
                    f"agent-routing.yaml has duplicate YAML key {key.value!r} "
                    f"in {scope}; refusing migration"
                )
            found[key.value] = (key, value)
        return found

    if not isinstance(root_node, MappingNode):
        return existing, [], []
    root_entries = entries(root_node, "document")
    route_node = root_entries.get("routes", (None, None))[1]
    if not isinstance(route_node, MappingNode):
        return existing, [], []
    route_entries = entries(route_node, "routes")
    routes = document["routes"]
    newline = "\r\n" if "\r\n" in existing else "\n"
    edits: list[tuple[int, int, str]] = []
    removed: list[str] = []

    def remove_default_route(name: str, expected: dict, block: str) -> None:
        if routes.get(name) != expected or name not in route_entries:
            return
        key_node, _value_node = route_entries[name]
        line_start = existing.rfind("\n", 0, key_node.start_mark.index) + 1
        exact = block.replace("\n", newline)
        if existing.startswith(exact, line_start):
            edits.append((line_start, line_start + len(exact), ""))
            removed.append(name)

    # Both defaults were shipped: the earlier Codex profile had no description.
    codex_base = (
        '  codex-evaluator:\n'
        '    agent: codex\n'
        '    args: ["--sandbox", "read-only"]\n'
    )
    codex_args = ["--sandbox", "read-only"]
    remove_default_route(
        "codex-evaluator", {"agent": "codex", "args": codex_args}, codex_base,
    )
    codex_description = (
        "Final Evaluation bằng Codex có sandbox read-only; "
        "người dùng có thể đổi args."
    )
    remove_default_route(
        "codex-evaluator",
        {"agent": "codex", "args": codex_args, "description": codex_description},
        codex_base + '    description: "' + codex_description + '"\n',
    )
    grouped_codex_description = (
        "Final Evaluation bằng Codex với profile read-only do user có thể sửa."
    )
    remove_default_route(
        "codex-evaluator",
        {
            "agent": "codex", "args": codex_args,
            "description": grouped_codex_description,
        },
        codex_base + '    description: "' + grouped_codex_description + '"\n',
    )

    # The October 2026 installer also generated an exact restricted Claude route.
    legacy_claude_args = [
        "--restricted", "--permission-mode", "dontAsk",
        "--tools", "Read,Glob,Grep",
        "--allowedTools", "Read,Glob,Grep",
        "--disallowedTools", "mcp__*",
        "--strict-mcp-config", "--disable-slash-commands",
    ]
    remove_default_route(
        "claude-evaluator",
        {"agent": "claude", "args": legacy_claude_args},
        '  claude-evaluator:\n'
        '    agent: claude\n'
        '    args: ' + json.dumps(legacy_claude_args) + '\n',
    )
    # The grouped effort-based defaults also shipped a YAML-aliased route.
    grouped_claude_description = (
        "Final Evaluation độc lập khi TaskGraph hoàn tất; "
        "đọc snapshot, đối chiếu toàn bộ requirements."
    )
    grouped_claude_args = [
        "--model", "claude-sonnet-5-5", "--effort", "high",
        "--permission-mode", "auto",
    ]
    remove_default_route(
        "claude-evaluator",
        {
            "agent": "claude", "args": grouped_claude_args,
            "description": grouped_claude_description,
        },
        '  claude-evaluator:\n'
        '    agent: claude\n'
        '    args: *sonnet_high_auto\n'
        '    description: "' + grouped_claude_description + '"\n',
    )

    # Remove the exact obsolete installer comment, not arbitrary user guidance.
    old_comment = (
        "# Final Evaluation is selected via start_final_evaluation API, "
        "not by route name." + newline
    )
    comment_at = existing.find(old_comment)
    if comment_at >= 0 and (comment_at == 0 or existing[comment_at - 1] == "\n"):
        edits.append((comment_at, comment_at + len(old_comment), ""))

    # Description ownership is independent of CLI arguments: users may change
    # model/effort while retaining the exact obsolete installer description.
    guidance = (
        (
            "claude-high",
            "Phân tích, planning, discovery có trọng tâm, testing nhiều nhánh "
            "và Final Evaluation thông thường.",
            "Phân tích, planning, discovery có trọng tâm, testing nhiều nhánh.",
        ),
        (
            "claude-xhigh",
            "Full discovery, debug khó, refactor lớn, security review "
            "và Final Evaluation nhiều contract.",
            "Full discovery, debug khó, refactor lớn, security review.",
        ),
    )
    for name, old_description, new_description in guidance:
        route = routes.get(name)
        route_entry = route_entries.get(name)
        if not isinstance(route, dict) or route.get("description") != old_description:
            continue
        if route_entry is None or not isinstance(route_entry[1], MappingNode):
            continue
        route_fields = entries(route_entry[1], name)
        description_entry = route_fields.get("description")
        if description_entry is None:
            continue
        value_node = description_entry[1]
        before = existing[value_node.start_mark.index:value_node.end_mark.index]
        if before.count(old_description) == 1:
            edits.append((
                value_node.start_mark.index, value_node.end_mark.index,
                before.replace(old_description, new_description, 1),
            ))

    # All source positions refer to the original YAML; apply from the end to
    # keep offsets stable and avoid accidentally editing an unrelated stanza.
    for start, end, replacement in sorted(edits, reverse=True):
        existing = existing[:start] + replacement + existing[end:]

    migrated = yaml.safe_load(existing)
    remaining_routes = migrated.get("routes", {}) if isinstance(migrated, dict) else {}
    remaining = sorted(
        name for name in remaining_routes
        if isinstance(name, str)
        and (
            "evaluator" in name.casefold().replace("_", "-")
            or "final-eval" in name.casefold().replace("_", "-")
        )
    ) if isinstance(remaining_routes, dict) else []
    return existing, removed, remaining

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
    claude_rules = root / "CLAUDE.md"
    codex = root / ".codex" / "config.toml"
    claude = root / ".mcp.json"
    for path in (agents, claude_rules, codex, claude, root / ".codex",
                 root / ".herdr-task-mcp", root / ".herdr-task-mcp" / ".gitignore",
                 root / "repos.yaml", root / "agent-routing.yaml"):
        if path.is_symlink():
            raise ValueError(f"refusing symlink: {path}")
    a_old = agents.read_bytes() if agents.exists() else b""
    lead_old = claude_rules.read_bytes() if claude_rules.exists() else b""
    c_old = codex.read_text(encoding="utf-8") if codex.exists() else ""
    m_old = claude.read_text(encoding="utf-8") if claude.exists() else ""
    route_file = root / "agent-routing.yaml"
    route_old = route_file.read_bytes().decode("utf-8") if route_file.exists() else None
    route_new, removed_routes, retained_routes = (
        migrate_legacy_evaluator_routes(route_old)
        if route_old is not None else (None, [], [])
    )
    # Complete all validations before modifying any file.
    herdr_session = (
        _validate_herdr_session(herdr_session)
        if herdr_session is not None
        else _existing_herdr_session(c_old, m_old)
    )
    a_new = managed_rules(a_old)
    lead_new = managed_claude_rules(lead_old)
    c_new = codex_config(c_old, python, root, herdr_session=herdr_session)
    m_new = claude_config(m_old, python, root, herdr_session=herdr_session)
    files = {
        agents: a_new,
        claude_rules: lead_new,
        codex: c_new.encode("utf-8"),
        claude: m_new.encode("utf-8"),
        root / ".herdr-task-mcp" / ".gitignore": b"*\n!.gitignore\n",
    }
    if route_new is not None and route_new != route_old:
        files[route_file] = route_new.encode("utf-8")
    for name, value in (("repos.yaml", REPOS), ("agent-routing.yaml", ROUTES)):
        path = root / name
        if not path.exists():
            files[path] = value.encode("utf-8")
    _commit_workspace_files(root, files)
    return {"workspace": str(root), "mcp": SERVER,
            "agents_marker": START, "python": str(python),
            "herdr_session": herdr_session,
            "removed_evaluator_routes": removed_routes,
            "retained_evaluator_routes": retained_routes}

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
