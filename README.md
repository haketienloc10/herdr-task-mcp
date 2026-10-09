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

## Test

`npm test` dùng fake Herdr runtime để kiểm thử điều phối và task lifecycle mà không cần khởi động agent thật. Integration test trên Herdr thực cần cả hai CLI và account đã xác thực.
