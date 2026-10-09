# herdr-task-mcp — QiQi Delegate standalone

Python MCP độc lập để delegate Codex/Claude qua Herdr. Không cần agent-knowledge-harness, không cài gì vào Git repo con, không chạy Supervisor Broker.

## Cài theo workspace, không cài global

Yêu cầu: Python >=3.10, Git, Herdr CLI có Codex/Claude integration.

Chạy tại workspace điều phối (`herdr-delegate-lab`). Git repo đích có thể nằm bên trong workspace hoặc là thư mục cùng cấp:

```bash
mkdir -p .tools
git clone -b feat/standalone-qiqi-delegate \
  https://github.com/haketienloc10/herdr-task-mcp.git .tools/herdr-task-mcp
# Chọn interpreter đã cài (>=3.10), ví dụ python3.12; KHÔNG dùng mặc định python3 nếu là 3.8.
PYTHON=python3.12
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "Requires Python >= 3.10"'
"$PYTHON" -m venv .tools/herdr-task-mcp/.venv
.tools/herdr-task-mcp/.venv/bin/python -m pip install --upgrade pip setuptools wheel
.tools/herdr-task-mcp/.venv/bin/python -m pip install -e './.tools/herdr-task-mcp'
.tools/herdr-task-mcp/.venv/bin/python -m qiqi_delegate.install --workspace "$PWD"
```

**Quan trọng với Ubuntu có `python3` mặc định là 3.8:** lệnh `python3 -m venv` sẽ tạo virtualenv Python 3.8, dù `pip` đã được nâng cấp. `mcp==2.1.1` và package này yêu cầu Python >=3.10; phải tạo lại virtualenv từ Python 3.10+. Kiểm tra bằng `.tools/herdr-task-mcp/.venv/bin/python --version`. Nếu virtualenv cũ đang dùng 3.8, chỉ xóa `.tools/herdr-task-mcp/.venv` rồi tạo lại bằng interpreter mới, không xóa `.tools/herdr-task-mcp`, workspace, `AGENTS.md` hay các Git repo con.

Nếu `pip install -e` báo thiếu `setup.py` thì `pip` trong virtualenv quá cũ. Bản này có `setup.py` tương thích, nhưng nên nâng cấp `pip`, `setuptools` và `wheel` theo lệnh trên trước khi cài. Sau đó mới chạy `qiqi_delegate.install`; không cần clone lại hoặc xóa workspace.

**Sửa lỗi MCP `connection closed` / `ModuleNotFoundError: qiqi_delegate`:** phiên bản installer cũ có thể dereference symlink `.venv/bin/python` về Python gốc do `uv` quản lý, khiến MCP chạy ngoài virtualenv. Sau khi `git -C .tools/herdr-task-mcp pull --ff-only`, chạy lại `.tools/herdr-task-mcp/.venv/bin/python -m qiqi_delegate.install --workspace "$PWD"`. Cấu hình `.codex/config.toml` và `.mcp.json` sẽ được sửa về đúng đường dẫn `.venv/bin/python`; không cần tạo lại virtualenv và mọi rule ngoài marker `AGENTS.md` vẫn giữ nguyên.

Installer chỉ cấu hình workspace: AGENTS.md, .codex/config.toml, .mcp.json, repos.yaml, agent-routing.yaml và .herdr-task-mcp/ (SQLite). Không sửa frontend/, backend/ hoặc cài MCP vào home/global.

## AGENTS.md — chỉ sửa nội dung nằm trong marker

```markdown
<!-- BEGIN HERDR-TASK-MCP RULES -->
... rules của QiQi Delegate ...
<!-- END HERDR-TASK-MCP RULES -->
```

Installer giữ nguyên mọi byte trước/sau marker, kể cả CRLF và khoảng trắng. Chạy lại không tạo block trùng. Nếu marker thiếu một bên, sai thứ tự hoặc bị nhân đôi, installer từ chối ghi. AGENTS.md trong repo con không được sửa.

## Repo registry ở workspace cha

Chỉnh `repos.yaml`:

```yaml
# Ví dụ: e2e-workspace/{herdr-delegate-lab,frontend,backend}
repositories:
  - name: frontend
    path: ../frontend
  - name: backend
    path: ../backend
```

`repository` là name trong registry. `path` luôn tương đối với `herdr-delegate-lab`, không phải đường dẫn tương đối với vị trí gọi MCP client. Runtime hỗ trợ Git repo nằm trong workspace (`frontend`) hoặc cùng cấp (`../frontend`). Target phải là **exact Git root**; không chấp nhận absolute path hoặc đường dẫn thoát khỏi thư mục cha của workspace. Không cần có file QiQi nào trong repo đích.

## Kiểm tra trước khi delegate

Chạy tại `herdr-delegate-lab` sau khi cấu hình `repos.yaml`:

```bash
.tools/herdr-task-mcp/.venv/bin/python - <<'PY'
from pathlib import Path
from qiqi_delegate.runtime import DelegateRuntime
runtime = DelegateRuntime(Path.cwd())
for name, git_root in runtime.repos().items():
    print(f"{name}: {git_root}")
PY
```

Đầu ra phải có hai exact Git root `frontend` và `backend`. Nếu registry sai, MCP trả lỗi `code=repository_registry_invalid` kèm lý do và hướng sửa, không còn chỉ báo `Error executing tool`.

Kiểm tra Herdr riêng: `herdr status server` và `herdr integration status`. Không cần sửa hoặc cài MCP vào repo đích. `start_graph` chỉ lưu TaskGraph; `delegate_next` mới khởi động Peer. Với bài khám phá độc lập, dùng hai node không có `depends_on` để chạy cùng wave, rồi review từng response.

## Herdr server: phiên hiện tại và headless fallback

Mặc định, MCP sử dụng Herdr session mà tiến trình Lead đang dùng: `HERDR_SOCKET_PATH` nếu có, hoặc `HERDR_SESSION`/default. MCP **không** tự tạo session `qiqi-<hash>` tách biệt nữa. Nếu Herdr server chưa chạy, runtime thử khởi động `herdr server` ở chế độ headless và đợi socket sẵn sàng trước khi tạo workspace. Đây không phải lệnh mở giao diện.

Không chạy `herdr session attach` từ một Codex/Claude đang ở trong Herdr; Herdr chặn nested TUI theo mặc định. Không bật `allow_nested` chỉ để khắc phục lỗi `server_not_running` của MCP. Khi muốn dùng một Herdr session riêng, có thể cấu hình rõ biến môi trường `QIQI_HERDR_SESSION` **ở MCP server**. Nếu không cấu hình, runtime sẽ dùng session hiện có. Khi lỗi khởi động kéo dài, kiểm tra `herdr status server`, đường dẫn socket và Herdr logs.

## Chọn Herdr session theo workspace

MCP chạy bên ngoài Herdr pane có thể không nhận `HERDR_SOCKET_PATH`. Khi Herdr sử dụng **named session**, không dùng socket default. Chỉ định session đang chạy trong cấu hình MCP thuộc workspace bằng installer, không sửa cấu hình global:

```bash
herdr --session <SESSION_NAME> status server
.tools/herdr-task-mcp/.venv/bin/python -m qiqi_delegate.install \
  --workspace "$PWD" --herdr-session <SESSION_NAME>
```

Ví dụ nếu Herdr server có socket `~/.config/herdr/sessions/qiqi-delegate/herdr.sock` thì `<SESSION_NAME>` là `qiqi-delegate`. Installer ghi `QIQI_HERDR_SESSION` vào `.codex/config.toml` và `.mcp.json` **của workspace**. Lần cài sau không truyền flag sẽ giữ lại tên session đã chọn. `AGENTS.md` chỉ được cập nhật trong marker do MCP quản lý. Không chạm vào repository đích.

Khởi động lại MCP client sau khi đổi cấu hình. Kiểm tra tên session ở `.codex/config.toml` và xác minh `herdr --session <SESSION_NAME> status server`. Khi đã chọn session đúng, không cần mở nested Herdr hoặc bật `allow_nested`.

## Danh sách repository và route

Gọi MCP `workspace_info` **trước** `start_graph` hoặc `delegate_repo_task`. Dùng một tên trong trường `route_names` làm tham số `route`; `routes` là ánh xạ *route name → agent kind*, vì thế giá trị `codex` là agent kind chứ không phải route hợp lệ. Tool này trả danh sách tên repository và route đã khai báo, cùng thông tin session Herdr. Không dùng tên route tự suy đoán. `start_graph` và `reconcile_graph` kiểm tra tất cả route trước khi ghi graph, nên route sai không tạo failed attempt.
## Chẩn đoán `agent_not_ready` (Peer bị chặn lúc startup)

Lỗi `agent_not_ready` xảy ra khi Herdr nhận diện agent, nhưng startup đang ở trạng thái `blocked` (có thể cần xử lý câu hỏi, đăng nhập hoặc trust prompt). **Không tự động nhấn Enter, cấp quyền hoặc gửi task prompt lại.** Đây không phải bằng chứng Peer đã xử lý task.

Bản runtime mới **giữ lại Herdr workspace và write claim** để có thể kiểm tra UI khởi động, thay vì đóng pane ngay và làm mất dấu vết. MCP trả `agent_name`, `pane_id`, `workspace_id`, `write_claim_id` cùng gợi ý kiểm tra; TaskGraph review cũng giữ `failure_detail` của runtime. `agent explain` dùng chẩn đoán trạng thái; `agent read` chỉ dùng xem startup UI, **không** dùng lấy báo cáo Peer/final response.

Chạy bằng session đã chọn trong `--herdr-session`:

```bash
herdr --session <SESSION_NAME> agent explain <AGENT_NAME> --json
herdr --session <SESSION_NAME> agent read <AGENT_NAME> --source visible --lines 30
herdr --session <SESSION_NAME> agent get <AGENT_NAME>
```

Nếu startup yêu cầu xác nhận, người dùng kiểm tra nội dung và quyết định thủ công. Nếu không thể tiếp tục, đóng workspace theo `workspace_id`:

```bash
herdr --session <SESSION_NAME> workspace close <WORKSPACE_ID>
```

Sau khi đã **xác nhận agent cũ kết thúc**, người vận hành cần xử lý write claim theo quy trình bảo trì riêng bằng đúng `repository` và `claim_id` từ lỗi. MCP không expose thao tác giải phóng claim. Không xóa SQLite hoặc giải phóng claim khi chưa xác nhận worker đã dừng. Sau khi xử lý an toàn mới tạo TaskGraph mới để chạy lại. Không sửa repository đích để khắc phục lỗi hạ tầng.

Nếu `agent_not_ready` xảy ra, `get_node_reviews` hiển thị `failure_detail` thay vì chỉ có `executor_exception`, giúp Lead báo chính xác blocker cho người dùng. Việc Herdr yêu cầu xác nhận trust/auth không thể được CI mock loại bỏ hoàn toàn: cần xác minh E2E trên môi trường Herdr thực.

## Decision contract và xử lý lỗi

- `start_graph` chỉ tạo TaskGraph. `delegate_next` mới chạy một wave của Peer.
- `get_node_reviews` cung cấp runtime state và evidence. `failed` vì hạ tầng không được xem là kết quả do Peer tạo; không ACCEPT.
- `submit_decisions(action="block")` yêu cầu `owner` và `return_checkpoint`. **Không** truyền `feedback` cho `block`, vì `feedback` chỉ dùng cho `retry`.
- `submit_decisions(action="retry")` cho phép `feedback` và `resume_session` để hướng dẫn lần chạy tiếp theo.
- Nếu bị `blocked`, Lead chỉ định người xử lý và checkpoint. Khi hạ tầng phục hồi, Lead cần replan/reconcile đúng node trước khi dispatch lại. Không tự đọc source hoặc chạy test trong repository đích thay Peer.
- Khi không có báo cáo Peer, phản hồi rõ lỗi và evidence hiện có; không trình bày kết quả tự khảo sát như kết quả delegation.
## Route và tham số agent

Chỉnh `agent-routing.yaml`: 

```yaml
routes:
  codex-balanced:
    agent: codex
    args: ['--yolo']
  claude-balanced:
    agent: claude
    args: ['--permission-mode', 'auto']
```

Đây là ví dụ opt-in. Installer mặc định để `args: []`. `--yolo` bỏ qua approval/sandbox của Codex; chỉ bật nếu workspace đã được cô lập. Runtime tự cấu hình native Stop hook nên không cho sửa hook bằng route args.

## Workflow

```text
Human → Lead QiQi → TaskPacket / TaskGraph → qiqi_delegate → Herdr
  → Codex/Claude Peer → Native Stop hook → captured result → Lead decision
```

**Direct Delegation:** `delegate_repo_task` nhận `repository`, `route`, `objective`, `scope`, `acceptance_criteria` và các field tùy chọn. Không có `session_id` thì START; có exact `session_id` được sở hữu hợp lệ thì RESUME.

**TaskGraph:** `workspace_info` → `start_graph` → `delegate_next` → `get_node_reviews` → `submit_decisions`. Có thể dùng `get_graph` hoặc `reconcile_graph` khi cần. Downstream chỉ chạy sau khi upstream được Lead ACCEPT. Tối đa một writer cùng repo trong một wave.

**Native result:** mỗi delegated turn có sink/nonce riêng, lấy final response từ Stop/StopFailure hook thay vì Herdr screen. Nếu nhiều Stop events tạo `capture_ambiguous`, result chỉ có `candidate_count`, không có authoritative `agent_response` hoặc `capture_review_id`; TaskGraph giữ attempt để review nhưng không cho ACCEPT. Lead cần RETRY, REPLAN hoặc BLOCK. SQLite tại `.herdr-task-mcp/qiqi_delegate.sqlite3` giữ session, turn và write claim. Khi cleanup không xác nhận, claim còn hiệu lực và phải được giải phóng thủ công sau khi worker cũ đã dừng.

**Không có Supervisor:** không chạy broker, supervisor agent hoặc case audit. SLP R1–R5 không nằm trong runtime độc lập. Lead chịu trách nhiệm technical review.

### TaskGraph restart recovery

- Authored DAG (bao gồm TaskPacket, route và dependency), node state, revision, attempts và pending retry plan được lưu vào SQLite. Sau khi server restart, `get_graph(graph_run_id)`, `get_node_reviews`, `submit_decisions`, `reconcile_graph` và `delegate_next` đọc lại dữ liệu này; không tự reset ACCEPT hoặc dispatch lại node.
- `submit_decisions(action="retry")` lưu TaskPacket kèm feedback và lựa chọn START/RESUME trong cùng transaction với semantic transition. `delegate_next` kiểm tra revision và claim **cả wave atomically**; nếu node thứ hai không claim được, toàn bộ wave rollback (không có attempt giả hoặc mất feedback).
- Attempt mới ở trạng thái dispatch `prepared` vẫn giữ retry plan trong SQLite; chỉ khi coroutine tới ranh giới dispatch mới tiêu thụ pending plan, đồng thời lưu `retry_plan_json` gốc trong attempt để audit. Crash trước hoặc sau ranh giới này đều **fail-closed** nếu wave còn `running`—không tự kết luận worker đã dừng hay tự replay.
- Nếu server chết sau khi mọi attempt đã terminal nhưng trước `close_wave()`, lần load kế tiếp tự đóng quiescent wave bằng transaction. Không tạo thêm attempt hoặc giả định Peer tạo kết quả mới.
- Nếu restart lúc wave đang chạy, attempt vẫn `running` và graph không `ready`. Không tự coi worker đã dừng, không xóa claim, không retry. Người vận hành phải kiểm tra và xử lý worker/claim theo quy trình an toàn trước khi mở lại graph. Không có MCP recovery tool.
- Graph run được tạo trước phiên bản lưu `graph_json` không thể khôi phục chỉ từ fingerprint. Runtime từ chối mở graph legacy thay vì tự suy đoán authored DAG. Nên tạo graph run mới hoặc di chuyển authored definition qua migration được kiểm chứng; không sửa SQLite bằng phỏng đoán.


## Test và giới hạn

```bash
.tools/herdr-task-mcp/.venv/bin/python -m pytest -q .tools/herdr-task-mcp/tests-standalone
```

CI kiểm thử Python 3.10/3.13. Chưa kiểm tra end-to-end với Herdr thực. Authored TaskGraph và retry plan vẫn giữ trong process memory; chưa phục hồi đầy đủ sau restart.

---

## Node task MCP trước đây

# herdr-task-mcp

MCP server dùng chung để **Codex ↔ Codex/Claude** và **Claude ↔ Codex/Claude** giao task thông qua [Herdr](https://herdr.dev/). Đây là MVP; chưa tự động merge worktree hay xác minh chất lượng code của worker.

## Kiến trúc

```text
Codex MCP client ─┐
                  ├─ stdio MCP adapter ── Unix socket (0600) ── Orchestrator daemon
Claude MCP client ┘                                             ├─ SQLite queue
                                                               └─ Herdr CLI → worker pane
```

Hai MCP client sử dụng chung daemon, thay vì mỗi MCP process giữ queue trong memory. Daemon lưu task qua các lần restart. Task đang RUNNING khi daemon restart chuyển sang BLOCKED để tránh chạy lặp gây side effect; worker Herdr cũ có thể vẫn đang hoạt động, phải kiểm tra thủ công.

## Yêu cầu

- Node.js >= 22.16; khuyến nghị Node.js 24; npm.
- Herdr CLI được cài và đang chạy; `herdr --version` và `herdr agent list` hoạt động.
- Codex CLI và/hoặc Claude Code CLI, đã đăng nhập và sẵn sàng trong Herdr.
- macOS/Linux (Unix-domain socket); Windows chưa được kiểm thử.

## Cài đặt theo workspace (không global)

Chỉ những workspace được cấu hình mới có MCP này. Mọi lệnh dưới đây chạy **từ thư mục gốc của workspace cần dùng**, không chạy `npm install -g`, `codex mcp add` hoặc `claude mcp add`.

```bash
cd /absolute/path/to/my-project
mkdir -p .tools
git clone -b feat/mcp-task-orchestrator https://github.com/haketienloc10/herdr-task-mcp.git .tools/herdr-task-mcp
npm --prefix .tools/herdr-task-mcp install
npm --prefix .tools/herdr-task-mcp run build
npm --prefix .tools/herdr-task-mcp test

node .tools/herdr-task-mcp/dist/src/cli.js workspace init
```

`workspace init [workspace-path]` tạo hoặc cập nhật đúng hai cấu hình **cấp project**:

- `.codex/config.toml` — Codex đọc khi project được đánh dấu **trusted**.
- `.mcp.json` — Claude Code đọc tại workspace và yêu cầu phê duyệt MCP theo thiết lập bảo mật của Claude.

**Codex direct MCP tools:** `workspace init` còn thêm setting sau vào `.codex/config.toml` của workspace:

```toml
[features.code_mode]
direct_only_tool_namespaces = ["mcp__herdr_task"]

[mcp_servers.herdr-task]
command = "node"
# args và env được tự tạo từ đường dẫn workspace
```

`mcp__herdr_task` là namespace Codex tạo từ server `herdr-task` (đổi dấu `-` thành `_`).
Setting này giữ các tool trong namespace đó ở dạng direct, không đưa qua Code Mode. `workspace init` không tự bật `features.code_mode.enabled`.
Nếu đã có `[features.code_mode]`, installer bổ sung namespace vào array hiện có và giữ các setting khác. Chạy lại không tạo trùng table/key.

Nội dung MCP dùng `node` chạy file CLI được build **bên trong workspace**, không thay đổi `~/.codex/config.toml`, `~/.claude.json` hoặc đăng ký MCP global. Nếu hai file đã có cấu hình khác, installer giữ nguyên cấu hình khác. Nếu có cấu hình `herdr-task` không do installer quản lý, installer báo lỗi thay vì ghi đè.

Trong một terminal Herdr tại thư mục gốc workspace, chạy daemon **riêng cho workspace đó**:

```bash
node .tools/herdr-task-mcp/dist/src/cli.js workspace daemon
```

Giữ daemon chạy khi dùng Codex/Claude. Dữ liệu chỉ nằm trong `<workspace>/.herdr-task-mcp/` (SQLite, Unix socket, report JSON). Thư mục dữ liệu chứa `.gitignore` để không commit SQLite/report. Daemon từ chối `task_submit.cwd` nằm ngoài workspace, kể cả đường dẫn symlink trỏ ra ngoài. Đây là kiểm tra phạm vi task, **không phải sandbox chống mã độc**.

Khởi động lại Codex/Claude đang mở workspace để đọc cấu hình. Kiểm tra bằng `codex mcp list` / `claude mcp list` hoặc `/mcp` trong Claude. Khi không cần MCP ở workspace khác, **không chạy `workspace init` tại đó**.

Nếu cài đặt chỉ phục vụ máy cá nhân, có thể thêm các đường dẫn cấu hình có chứa absolute path vào `.git/info/exclude` của project (hoặc `.gitignore`) để tránh commit. Ví dụ:

```bash
printf '%s\n' '.tools/herdr-task-mcp/' '.codex/config.toml' '.mcp.json' >> .git/info/exclude
```

Lưu ý: ignore không áp dụng cho file đã được Git track. Các path absolute trong cấu hình tạo ra từ `workspace init` không dùng chung trực tiếp giữa nhiều máy; mỗi máy nên chạy lệnh init tại workspace tương ứng.

Nếu trước đó đã đăng ký MCP ở user/global config, hãy kiểm tra và gỡ bản đăng ký cũ riêng. `workspace init` không tự sửa cấu hình cá nhân của Codex hoặc Claude.

## Tham số khởi động agent theo workspace

`workspace init` tạo `.herdr-task-mcp/settings.json` trong workspace và không ghi đè khi chạy lại. Mặc định `args` rỗng, để quyền phê duyệt và sandbox do Codex/Claude quản lý. Nếu muốn worker tự thực hiện chỉnh sửa file, sửa file như sau:

```json
{
  "agents": {
    "codex": { "args": ["--yolo"] },
    "claude": { "args": ["--permission-mode=auto"] }
  }
}
```

Herdr khởi chạy worker qua `herdr agent start <name> --kind codex --pane <pane-id> --timeout 30000 -- --yolo` hoặc cùng lệnh với `--kind claude` và `-- --permission-mode=auto`. Dấu `--` phân tách tham số Herdr và tham số của agent. Có thể truyền các cặp tham số dưới dạng từng phần tử riêng, ví dụ `"args": ["--model", "model-name"]`.

- **Phạm vi:** settings chỉ áp dụng cho worker thuộc daemon workspace này, không thay đổi cài đặt Codex/Claude toàn cục hay MCP client.
- **Thời điểm áp dụng:** daemon đọc lại file khi bắt đầu mỗi worker mới. Có thể sửa file trong lúc daemon chạy; task đã chạy không thay đổi.
- **Xác thực:** JSON lỗi, key không biết hoặc `args` không phải string array sẽ khiến task mới thất bại trước khi tạo pane. Xem `task_result.error` để biết nguyên nhân.
- **Git:** `.herdr-task-mcp/settings.json` nằm trong thư mục dữ liệu bị `.gitignore` nội bộ bỏ qua. Nếu muốn chia sẻ cấu hình, copy riêng sau khi xem xét quyền và môi trường.
- **Bảo mật:** `--yolo` bỏ qua cơ chế phê duyệt/sandbox của Codex, chỉ dùng trong môi trường tin cậy hoặc đã cô lập. `--permission-mode=auto` của Claude phụ thuộc phiên bản và quyền sử dụng; kiểm tra `claude --help` trước. Không lưu token hoặc secret vào `args`.

Không cần chạy lại `workspace init` sau mỗi lần chỉnh sửa. Nếu mới cập nhật mã nguồn của MCP, chạy lại `npm run build`, dừng daemon cũ và khởi động daemon mới để nạp implementation.
## Workflow

Từ Codex hoặc Claude, gọi `task_submit`:

```json
{
  "target": "claude",
  "mode": "review",
  "cwd": "/absolute/path/to/project",
  "instruction": "Review the authentication module for correctness and security",
  "timeout_ms": 300000
}
```

Kết quả `QUEUED` có `id`. Dùng `task_wait({"task_id":"<id>"})` hoặc `task_status` để theo dõi; sau đó `task_result` lấy summary/report path. `task_cancel` hủy task, `task_list` liệt kê queue.

Nếu MCP chạy trong pane Herdr, adapter tự lấy `HERDR_PANE_ID`, rồi tạo worker bằng `herdr pane split`. Ngoài Herdr pane, daemon tạo workspace mới. Khi một worker giao task con, adapter đọc `HERDR_TASK_PARENT_ID` nếu được truyền qua pane; worker cũng phải gửi `parent_task_id` như prompt hướng dẫn. `maxDepth=1` theo mặc định.

### Trạng thái task

- `QUEUED`: chờ dependency hoặc slot.
- `RUNNING`: worker đang thực thi.
- `SUCCEEDED`: worker đã tự ghi report `outcome=success`; **không phải đã được test/verify độc lập**.
- `FAILED`: worker tự báo failure hoặc Herdr CLI lỗi.
- `BLOCKED`: cần phê duyệt trong pane, thiếu report, lỗi timeout hoặc dependency không thành công.
- `CANCELLED`: đã yêu cầu dừng. Worker có thể cần xử lý thêm nếu Ctrl+C không hiệu lực.

Worker phải ghi file report JSON đúng `task_id`. Herdr chỉ biết agent đã về trạng thái `idle`/`done`, không bảo đảm nó làm xong task. Vì vậy daemon **không** suy ra thành công chỉ từ `agent prompt --wait`.

## Cấu hình

| Environment variable | Mặc định | Ý nghĩa |
|---|---|---|
| `HERDR_TASK_DATA_DIR` | `~/.herdr-task-mcp` với `daemon` cũ; `<workspace>/.herdr-task-mcp` với `workspace daemon` | SQLite, Unix socket, report |
| `HERDR_TASK_SOCKET` | `<dataDir>/orchestrator.sock` | Đường dẫn Unix socket; đồng nhất giữa daemon và adapters |
| `HERDR_TASK_WORKSPACE_ROOT` | Không giới hạn nếu không cấu hình; được đặt tự động với `workspace` | Cấm task `cwd` nằm ngoài root |
| `HERDR_BIN` | `herdr` | Path đến CLI Herdr |
| `HERDR_TASK_MAX_CONCURRENT` | `3` | Số worker hoạt động đồng thời |
| `HERDR_TASK_MAX_DEPTH` | `1` | Độ sâu task con |
| `HERDR_TASK_MAX_CHILDREN` | `6` | Số task con tối đa cho một task |
| `HERDR_TASK_POLL_MS` | `500` | Chu kỳ scheduler (ms) |

Scheduler giữ tối thiểu một slot cho task con. Các task root vì vậy dùng tối đa `MAX_CONCURRENT-1` slot. Nếu yêu cầu phụ thuộc tạo chu kỳ thủ công giữa nhiều task, daemon chưa hỗ trợ sửa dependencies của task sau khi tạo; một task mới chỉ có thể phụ thuộc task đã tồn tại.

## Giới hạn MVP và lưu ý an toàn

- **Cần kiểm tra quyền truy cập:** Unix socket giới hạn ở user local, không expose TCP. Chỉ cho agent tin cậy kết nối.
- **Cùng working tree:** worker trong pane mới dùng `cwd` được chọn; **chưa tự tạo Git worktree riêng**. Không giao đồng thời nhiều implementation chạm cùng file. Review/research có thể chạy song song.
- Worker ghi report là lời tự khai. Trước khi merge cần test, review diff và xác thực commit.
- `BLOCKED` không có lệnh resume tự động; thao tác trực tiếp trong pane Herdr nếu cần phê duyệt.
- Khi daemon restart, task RUNNING chuyển BLOCKED; kiểm tra worker cũ tránh duplicate changes.
- Dữ liệu SQLite và prompt có thể chứa thông tin dự án; không commit thư mục dữ liệu vào Git.

## Kết quả dài hơn terminal screen

Herdr đọc các dòng đã render; màn hình alternate-screen của Claude Code có thể chỉ hiển thị một phần nội dung. `agent read --lines N` không phải giao thức truyền kết quả đầy đủ. `herdr-task-mcp` không phụ thuộc vào việc đọc lại toàn bộ màn hình worker.

Từ phiên bản này, adapter **gửi prompt một lần** (không dùng `agent prompt --wait`) rồi chờ **file JSON report hợp lệ** tại `.herdr-task-mcp/reports/<task-id>.json`. Mỗi 2 giây, adapter kiểm tra `agent get` để phát hiện trạng thái `blocked`. `idle` và `done` không chứng minh task thành công. Nếu quá `timeout_ms` mà không có report hợp lệ, task chuyển `BLOCKED` để kiểm tra thủ công; adapter không tự động gửi lại prompt.

Worker được hướng dẫn giữ terminal response dưới một dòng ngắn, JSON `summary` dưới 600 ký tự và ghi nội dung dài (test log, phân tích, review) vào `<task-id>.md`, đặt đường dẫn đó trong `artifacts` của JSON report. Report được viết atomic và cuối cùng, sau khi hoàn tất code/test/commit.

Chỉ đọc terminal qua `agent read` để chẩn đoán khi không có report; kết quả chính lấy từ report. Cần xác minh end-to-end trên phiên bản Herdr và Codex/Claude đang dùng.
## Chẩn đoán lỗi khởi chạy Codex worker

Nếu backend Claude hoàn thành nhưng frontend Codex thất bại trước khi chạy task, kiểm tra `task_status` hoặc `task_result` của task F1. Khi `agent start` lỗi, task giữ `pane_id` và màn hình terminal cuối cùng để hỗ trợ chẩn đoán.

Kiểm tra output trong pane worker (thay `<pane_id>` bằng `pane_id` trả về):

```bash
herdr pane read <pane_id> --source recent-unwrapped --lines 120
```

Kiểm tra từ **một pane Herdr mới** (không chỉ shell đang chạy coordinator):

```bash
command -v codex
codex --version
command -v bwrap || true
bwrap --version 2>&1 || true
printf 'PATH=%s\\nSHELL=%s\\n' "$PATH" "$SHELL"
```

- Nếu `codex` không có trong `PATH`: sửa môi trường khởi chạy **Herdr server**, sau đó restart Herdr server và tạo pane mới. Server có thể giữ `PATH` từ khi khởi chạy.
- Nếu `codex --version` chạy nhưng Codex không spawn được shell: kiểm tra nguyên văn lỗi từ `pane read`, đường dẫn shell/sandbox được nêu trong log và quyền thực thi. Không mặc định coi đây là lỗi task scheduler.
- Nếu lỗi nhắc tới `bwrap` (Bubblewrap), kiểm tra executable, sandbox backend và quyền Linux/WSL. Không tự động tắt sandbox hoặc dùng `--dangerously-bypass-approvals-and-sandbox`.
- Nếu pane worker không có interactive shell, xem cấu hình Herdr `[terminal].default_shell` và `shell_mode`. `agent start` cần pane đang ở shell prompt.

Sau khi sửa môi trường, tạo **task F1 mới** với cùng instruction, không khởi chạy lại B1 đã hoàn thành. Hệ thống không tự retry task `FAILED`.

## Test

`npm test` dùng fake Herdr runtime để kiểm thử điều phối và task lifecycle mà không cần khởi động agent thật. Integration test trên Herdr thực cần cả hai CLI và account đã xác thực.
