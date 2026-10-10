# herdr-task-mcp

`herdr-task-mcp` cung cấp hai cách giao việc cho Codex và Claude qua Herdr. Repository gồm MCP Python `qiqi_delegate` và dịch vụ điều phối Node.js. Hai cách chạy có API, vòng đời và dữ liệu riêng.

Tài liệu này mô tả hành vi được xác định từ mã nguồn. Các lệnh dùng đường dẫn ví dụ. Thay chúng bằng đường dẫn thực tế trước khi chạy.

## Chọn cách chạy

| Thành phần | Khi nào dùng | Giao tiếp | Dữ liệu |
|---|---|---|---|
| `qiqi_delegate/` (Python) | Lead QiQi giao việc theo repository, review evidence và quản lý TaskGraph | MCP qua stdio; gọi Herdr CLI | `.herdr-task-mcp/qiqi_delegate.sqlite3` trong workspace điều phối |
| `src/` (Node.js) | Client cần hàng đợi task, daemon và API `task_*` | MCP qua stdio → HTTP trên Unix socket → daemon | `tasks.sqlite`, `reports/` và `settings.json` trong thư mục dữ liệu |

**Hai thành phần không dùng chung task ID hoặc trạng thái.** Không chuyển `graph_run_id` của Python sang công cụ `task_*` của Node.js.

Các phần dưới đây hướng dẫn `qiqi_delegate` trước. Dịch vụ Node.js có hướng dẫn riêng ở cuối.

## Yêu cầu

Để chạy `qiqi_delegate`, chuẩn bị:

- Python **3.10 trở lên** và `pip`.
- `git` và các Git repository đã tồn tại.
- Herdr CLI (`herdr`) có thể chạy trong môi trường MCP.
- Codex hoặc Claude đã được cấu hình để Herdr khởi động Peer tương ứng.
- Một **workspace điều phối** riêng để lưu cấu hình và trạng thái.

Không cần Node.js để chạy `qiqi_delegate`. Dịch vụ Node.js yêu cầu **Node.js 22.16.0 trở lên**.

## 1. Cài MCP Python

Chạy các lệnh trong terminal:

~~~bash
git clone https://github.com/haketienloc10/herdr-task-mcp.git
cd herdr-task-mcp
python3 -m venv .venv
.venv/bin/python -m pip install .
mkdir -p ../qiqi-control
.venv/bin/python -m qiqi_delegate.install --workspace ../qiqi-control
~~~

`qiqi_delegate.install` đăng ký MCP cho **workspace điều phối**. Công cụ không cài code vào các repository nhận việc.

Sau khi cài, kiểm tra các đường dẫn:

~~~text
qiqi-control/
├── AGENTS.md
├── .codex/config.toml
├── .mcp.json
├── repos.yaml
├── agent-routing.yaml
└── .herdr-task-mcp/
    └── .gitignore
~~~

- `AGENTS.md` chứa quy tắc của Lead trong một vùng có marker.
- `.codex/config.toml` đăng ký `mcp_servers.qiqi_delegate`.
- `.mcp.json` đăng ký `mcpServers.qiqi_delegate`.
- `repos.yaml` khai báo các Git root được phép nhận việc.
- `agent-routing.yaml` khai báo route đến Codex hoặc Claude.
- `.herdr-task-mcp/` giữ dữ liệu chạy. SQLite được tạo khi runtime khởi động.

Installer chỉ thay vùng marker trong `AGENTS.md`. Với Codex, installer giữ cấu hình không liên quan và có thể bổ sung `features.code_mode.direct_only_tool_namespaces`. Installer từ chối marker hoặc cấu hình không hợp lệ thay vì ghi đè tùy ý.

Muốn dùng một Herdr session cụ thể, thêm tùy chọn khi cài:

~~~bash
.venv/bin/python -m qiqi_delegate.install --workspace ../qiqi-control --herdr-session dev-session
~~~

Nếu không đặt `--herdr-session`, runtime dùng Herdr socket/session do môi trường cung cấp. Khi cài lại, installer có thể giữ session đã khai báo trong cấu hình MCP.

## 2. Đăng ký repository và route

### `repos.yaml`

Mở `../qiqi-control/repos.yaml`. Khai báo các repository đã có trên máy:

~~~yaml
repositories:
  - name: backend
    path: ../backend
  - name: frontend
    path: ../frontend
~~~

Đường dẫn được tính từ workspace điều phối. Repository có thể nằm trong workspace hoặc nằm cạnh workspace, dưới cùng thư mục cha.

Runtime kiểm tra từng đường dẫn trước khi giao việc:

- Đường dẫn phải tương đối và trỏ đúng **Git root**, không trỏ đến thư mục con.
- Git root phải tồn tại trong phạm vi được cho phép.
- Hai tên không được trỏ đến cùng Git root, kể cả qua symlink.
- Runtime từ chối đường dẫn vượt ra ngoài thư mục cha của workspace điều phối.

**Không dùng tên repository để suy đoán đường dẫn.** Dùng đúng giá trị `name` đã đăng ký.

### `agent-routing.yaml`

Mở `../qiqi-control/agent-routing.yaml`:

~~~yaml
routes:
  codex-balanced:
    agent: codex
    args: []
  claude-balanced:
    agent: claude
    args: []
~~~

`route` là khóa `codex-balanced` hoặc `claude-balanced`. Giá trị `agent` chỉ xác định loại Peer.

Các phần tử trong `args` là tham số CLI. Runtime tự cấu hình native Stop hook. Không đưa tùy chọn ghi đè hook vào `args`.

**CAUTION:** Không bật các tùy chọn bỏ qua approval hoặc sandbox nếu chưa kiểm tra ranh giới quyền truy cập. Peer có thể sửa file trong Git repository được giao.

## 3. Kiểm tra MCP

Khởi động lại phiên Codex hoặc Claude trong workspace điều phối. Client đọc cấu hình MCP do installer tạo.

Gọi công cụ `workspace_info` trước. Kiểm tra các trường:

- `repositories`: tên repository đang hợp lệ.
- `route_names`: route có thể dùng.
- `routes`: ánh xạ route sang `codex` hoặc `claude`.
- `herdr_session`: session được chọn.
- `herdr_socket_env_present`: trạng thái biến `HERDR_SOCKET_PATH`.

MCP Python được chạy bằng `python -m qiqi_delegate.server`. Nó dùng stdio; không phải HTTP server công khai.

Nếu Herdr server chưa sẵn sàng, runtime thử khởi động `herdr server` ở chế độ headless. Không dùng `herdr session attach` bên trong một Herdr pane khác.

## 4. Giao một TaskPacket

`TaskPacket` mô tả công việc cho một Peer. Ba trường bắt buộc là `objective`, `scope` và `acceptance_criteria`.

Ví dụ input cho công cụ `delegate_repo_task`:

~~~json
{
  "repository": "backend",
  "route": "codex-balanced",
  "objective": "Kiểm tra luồng xử lý yêu cầu tạo đơn hàng",
  "scope": ["src/api", "tests"],
  "acceptance_criteria": [
    "Chỉ rõ entry point và luồng dữ liệu bằng file:line",
    "Nêu các trường hợp lỗi và cách kiểm chứng",
    "Ghi rõ test nào đã chạy hoặc chưa chạy"
  ],
  "constraints": ["Không sửa file trong lần phân tích này"]
}
~~~

Có thể thêm `out_of_scope`, `context`, `constraints` và `known_unknowns`. Trong `context`, phân biệt `trusted_facts` với `claims_to_investigate`. Mỗi mục có `source`.

Nếu không truyền `session_id`, `delegate_repo_task` mở lần chạy mới. Nếu truyền `session_id`, runtime kiểm tra session thuộc đúng repository và agent trước khi RESUME.

**Peer chỉ làm việc trong Git root được giao.** Lead không tự đọc hoặc sửa repository đích để thay Peer. Khi cần thông tin từ repository khác, Lead giao TaskPacket riêng.

## 5. Điều phối nhiều TaskPacket bằng TaskGraph

`TaskGraph` là đồ thị không chu trình (DAG). Mỗi node có `node_id`, `repository`, `route`, `task_packet` và `depends_on`.

Ví dụ input cho `start_graph`:

~~~json
{
  "graph": {
    "nodes": [
      {
        "node_id": "api",
        "repository": "backend",
        "route": "codex-balanced",
        "task_packet": {
          "objective": "Xác định contract API đơn hàng",
          "scope": ["src/api"],
          "acceptance_criteria": ["Cung cấp contract và file:line"]
        }
      },
      {
        "node_id": "web",
        "repository": "frontend",
        "route": "claude-balanced",
        "depends_on": ["api"],
        "task_packet": {
          "objective": "Đối chiếu client với contract đã được Lead chấp nhận",
          "scope": ["src"],
          "acceptance_criteria": ["Nêu phần tương thích và sai khác"]
        }
      }
    ]
  }
}
~~~

`start_graph` chỉ kiểm tra và lưu TaskGraph. Nó **không** khởi động Peer.

### Trình tự chạy

1. Gọi `start_graph` và lưu `graph_run_id`.
2. Gọi `get_graph` để đọc `graph_state`, `revision` và `runnable_nodes`.
3. Gọi `delegate_next` để chạy một wave không xung đột.
4. Gọi `get_graph` để nhận `review_required` và các `attempt_id`.
5. Gọi `get_node_reviews` với đúng `node_id` và `attempt_id`.
6. Đối chiếu evidence với từng `acceptance_criteria`.
7. Gọi `submit_decisions` với `expected_revision` mới nhất.
8. Lặp lại từ `get_graph` nếu TaskGraph còn công việc.

`get_node_reviews` nhận tối đa **8** cặp review locator trong một lần gọi. `expected_revision` giúp từ chối quyết định dựa trên snapshot cũ.

Node phụ thuộc chỉ chạy sau khi Lead **ACCEPT** các node trước đó. Các node độc lập có thể chạy đồng thời khi không tranh chấp Git root hoặc session RESUME.

### Quyết định của Lead

| `action` trong API | Ý nghĩa | Dữ liệu bổ sung |
|---|---|---|
| `accept` | Chấp nhận evidence của attempt `settled`; node thành `satisfied` | Không dùng metadata của retry |
| `retry` | Đưa node về `pending` để chạy lại | Có thể dùng `feedback` và `resume_session` |
| `replan` | Chặn topology hiện tại để Lead sửa TaskGraph | Bắt buộc `owner` và `return_checkpoint` |
| `block` | Chặn node khi chưa thể tiếp tục | Bắt buộc `owner` và `return_checkpoint` |

`submit_decisions` ghi quyết định và retry plan vào SQLite. `get_graph.nodes[].last_lead_decision` cho phép đọc lại metadata sau restart.

Dùng `reconcile_graph` cùng `expected_revision` khi cần thay đổi topology. Runtime giữ lịch sử attempt và chỉ reset các node bị thay đổi hoặc phụ thuộc vào thay đổi theo quy tắc reconcile.

## 6. Evidence và trạng thái lỗi

`qiqi_delegate.result_hook` nhận sự kiện native Stop từ Codex hoặc Claude. Runtime dùng kết quả capture này để tạo báo cáo Peer. Runtime không coi nội dung terminal là nguồn thay thế cho final response.

Phân biệt hai lớp trạng thái:

- **Runtime state** cho biết attempt đã `settled`, `failed`, `blocked` hoặc còn `running`.
- **Semantic decision** do Lead chọn: ACCEPT, RETRY, REPLAN hoặc BLOCK.

`settled` **không tự động** đồng nghĩa ACCEPT. Runtime cũng từ chối ACCEPT nếu attempt không có captured Peer evidence hợp lệ.

Nếu có nhiều kết quả capture không xác định được một câu trả lời duy nhất, API báo `capture_ambiguous`. Lead phải review và chọn RETRY, REPLAN hoặc BLOCK. Không ACCEPT kết quả mơ hồ.

Các lỗi công khai có thể chứa mã như `repository_registry_invalid`, `routing_invalid`, `worker_runtime_failed` hoặc `agent_startup_blocked`. Đọc nguyên nhân và hành động khắc phục trong lỗi trước khi thử lại.

## 7. Phục hồi sau khi worker hoặc MCP dừng

**CAUTION: Không giải phóng write claim khi worker còn chạy.** Hai writer trên cùng Git root có thể ghi đè thay đổi của nhau.

Runtime dùng SQLite write claim theo **Git root đã chuẩn hóa**. Nếu tên repository đổi trong `repos.yaml`, claim cũ vẫn chặn writer mới trên cùng Git root.

### Phục hồi write claim

1. Kiểm tra Herdr pane và xác định worker liên quan.
2. Dừng worker hoặc đóng workspace Herdr nếu cần.
3. Xác nhận worker không còn ghi vào Git root.
4. Đọc `claim_id` thực tế:

~~~bash
.venv/bin/python -m qiqi_delegate.maintenance show-claim --workspace ../qiqi-control --repository backend
~~~

5. Chỉ sau khi xác nhận worker đã dừng, giải phóng đúng claim:

~~~bash
.venv/bin/python -m qiqi_delegate.maintenance release-claim --workspace ../qiqi-control --repository backend --claim-id 'turn:<exact-id>' --worker-termination-confirmed
~~~

Lệnh ghi audit vào `write_claim_recovery_audit`. Không dùng claim ID phỏng đoán.

Khi nâng cấp dữ liệu cũ, claim chưa có `repository_root` sẽ chặn các delegation mới. Operator phải xác minh worker đã dừng và giải phóng claim theo ID cũ. Runtime không tự ánh xạ claim này bằng tên mới trong `repos.yaml`.

### Phục hồi TaskGraph attempt

Nếu MCP dừng khi một attempt còn `running`, TaskGraph không tự phát lại công việc. Đọc `graph_run_id` và `attempt_id` từ `get_graph`.

Kiểm tra attempt:

~~~bash
.venv/bin/python -m qiqi_delegate.maintenance show-attempt --workspace ../qiqi-control --repository backend --graph-run-id '<graph-run-id>' --attempt-id '<attempt-id>'
~~~

Sau khi xác nhận worker đã dừng, xử lý mọi write claim liên quan. Sau đó phục hồi đúng attempt:

~~~bash
.venv/bin/python -m qiqi_delegate.maintenance recover-attempt --workspace ../qiqi-control --repository backend --graph-run-id '<graph-run-id>' --wave-id '<wave-id>' --node-id '<node-id>' --attempt-id '<attempt-id>' --worker-termination-confirmed
~~~

Runtime kiểm tra graph, wave, node, attempt và repository. Runtime đánh dấu attempt `failed` và ghi `graph_attempt_recovery_audit` trong cùng SQLite transaction.

Wave chỉ đóng khi không còn attempt `running`. Lead phải review trạng thái mới trước khi RETRY hoặc REPLAN.

**`qiqi_delegate.maintenance` là CLI cho operator, không phải MCP tool.** Cờ `--worker-termination-confirmed` là xác nhận thủ công. Cờ này không tự kiểm tra tiến trình Herdr.

## 8. Dịch vụ Node.js: API `task_*`

`src/` triển khai một cơ chế khác. Daemon nhận task, lưu vào `TaskStore`, kiểm tra dependency và giao việc qua `HerdrRuntime`. Client MCP gửi request đến daemon bằng Unix socket.

Chạy từ thư mục repository đã clone:

~~~bash
npm install
npm run build
node dist/src/cli.js workspace init /duong/dan/workspace
node dist/src/cli.js workspace daemon /duong/dan/workspace
~~~

Giữ terminal chạy `workspace daemon`. `workspace init` đăng ký MCP tên `herdr-task` trong cấu hình Codex và Claude của workspace.

Các công cụ Node.js:

| Tool | Tác dụng |
|---|---|
| `task_submit` | Đưa task vào hàng đợi, trả task ID |
| `task_status` | Đọc trạng thái task |
| `task_wait` | Chờ trạng thái cuối, mỗi lần tối đa 20 giây |
| `task_result` | Đọc tóm tắt và đường dẫn report |
| `task_cancel` | Yêu cầu hủy task và các task con |
| `task_list` | Liệt kê task đã lưu |

Daemon lưu task trong `.herdr-task-mcp/tasks.sqlite` của workspace. Peer ghi report JSON theo contract `WorkerReport` với `task_id`, `outcome` và `summary`.

Có thể chỉnh tham số khởi động Peer trong `.herdr-task-mcp/settings.json`. Cấu hình mặc định dùng `args: []` cho Codex và Claude. Daemon kiểm tra nội dung file trước khi khởi động worker.

Các biến `HERDR_TASK_MAX_CONCURRENT`, `HERDR_TASK_MAX_DEPTH`, `HERDR_TASK_MAX_CHILDREN` và `HERDR_TASK_POLL_MS` điều chỉnh giới hạn điều phối. `HERDR_BIN` chọn Herdr CLI cho dịch vụ Node.js.

## 9. Kiểm thử và vị trí mã nguồn

### Kiểm thử MCP Python

~~~bash
.venv/bin/python -m pip install '.[test]'
.venv/bin/python -m pytest -q tests-standalone
~~~

### Kiểm thử dịch vụ Node.js

~~~bash
npm install
npm run typecheck
npm test
npm run build
~~~

Các workflow CI nằm trong `.github/workflows/ci.yml` và `.github/workflows/qiqi-python.yml`. Workflow Python chạy bộ test trên Python 3.10, 3.12 và 3.13.

| Đường dẫn | Trách nhiệm |
|---|---|
| `qiqi_delegate/install.py` | Cài MCP và cập nhật cấu hình workspace |
| `qiqi_delegate/server.py` | Định nghĩa công cụ MCP Python |
| `qiqi_delegate/core.py` | Kiểm tra TaskPacket và xử lý native capture |
| `qiqi_delegate/result_hook.py` | Ghi sự kiện native Stop vào vùng capture |
| `qiqi_delegate/runtime.py` | Gọi Herdr, quản lý session và write claim |
| `qiqi_delegate/task_graph_scheduler.py` | Tính trạng thái và dependency |
| `qiqi_delegate/task_graph_runtime.py` | Điều phối wave, review và quyết định Lead |
| `qiqi_delegate/task_graph_store.py` | Lưu TaskGraph, attempt, revision và audit vào SQLite |
| `qiqi_delegate/maintenance.py` | Phục hồi có kiểm soát dành cho operator |
| `src/cli.ts`, `src/mcp.ts` | Điểm vào CLI và MCP Node.js |
| `src/orchestrator.ts`, `src/store.ts` | Điều phối hàng đợi và lưu task Node.js |
| `tests-standalone/`, `tests/` | Regression tests cho Python và Node.js |

## Giới hạn cần nhớ

- Không coi trạng thái Peer `settled` hoặc `SUCCEEDED` là xác nhận kỹ thuật độc lập.
- Không dùng terminal scraping để thay native capture của `qiqi_delegate`.
- Không tự động giải phóng claim hoặc phát lại attempt bị gián đoạn.
- Không sửa `.herdr-task-mcp` bằng tay để xử lý task kẹt.
- Không cấp quyền đọc hoặc ghi repository khác chỉ vì đường dẫn xuất hiện trong TaskPacket.
- Không dùng API `task_*` của Node.js thay cho `TaskGraph` của Python.

Khi có lỗi khởi động yêu cầu trust hoặc authentication, kiểm tra Herdr pane bằng operator. Chỉ tiếp tục sau khi người vận hành đã xác nhận trạng thái worker.
