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

## Cài đặt

```bash
npm install
npm run build
npm test
```

Mở một terminal trong môi trường Herdr và **chạy daemon một lần**:

```bash
node dist/src/cli.js daemon
```

Giữ daemon chạy. Mặc định dữ liệu nằm ở `~/.herdr-task-mcp/` (SQLite, socket và report JSON). MCP client chỉ kết nối đến daemon đã chạy.

Đăng ký MCP cho **cả hai CLI** (đổi `<absolute-repo-path>` thành đường dẫn tuyệt đối):

```bash
codex mcp add herdr-task -- node <absolute-repo-path>/dist/src/cli.js mcp
claude mcp add herdr-task -- node <absolute-repo-path>/dist/src/cli.js mcp
```

Kiểm tra `codex mcp list` / `claude mcp list`, rồi khởi động lại phiên Codex và Claude. Nếu CLI trên máy bạn khác cú pháp, xem `codex mcp add --help` và `claude mcp add --help`.

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
| `HERDR_TASK_DATA_DIR` | `~/.herdr-task-mcp` | SQLite, Unix socket, report |
| `HERDR_TASK_SOCKET` | `<dataDir>/orchestrator.sock` | Đường dẫn Unix socket; đồng nhất giữa daemon và adapters |
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
