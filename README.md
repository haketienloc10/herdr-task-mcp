# qiqi_delegate

`qiqi_delegate` là MCP Python để giao công việc cho Codex và Claude qua Herdr. Một **Lead** lập `TaskPacket`, chọn Git repository và review kết quả từ **Peer**. Lead quyết định ACCEPT, RETRY, REPLAN hoặc BLOCK.

Runtime Python tự quản lý TaskGraph, SQLite và write claim. Không cần daemon điều phối khác. Toàn bộ cấu hình MCP nằm trong **workspace điều phối**, không nằm trong repository nhận việc.

## Yêu cầu

- Python **3.10 trở lên**.
- `pip`, `git` và Git repository đã được tạo.
- Herdr CLI (`herdr`) có trong `PATH` hoặc được chỉ định bằng `QIQI_HERDR_BIN`.
- Codex hoặc Claude đã được Herdr cấu hình để khởi động.
- Một thư mục workspace dành riêng cho Lead.

## Cài đặt

Clone mã nguồn và cài package:

~~~bash
git clone https://github.com/haketienloc10/herdr-task-mcp.git
cd herdr-task-mcp
python3 -m venv .venv
.venv/bin/python -m pip install .
~~~

Tạo workspace điều phối. Ví dụ dưới đây đặt workspace cạnh thư mục mã nguồn:

~~~bash
mkdir -p ../qiqi-control
.venv/bin/python -m qiqi_delegate.install --workspace ../qiqi-control
~~~

Installer tạo hoặc cập nhật:

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

- `AGENTS.md` chứa quy tắc Lead trong cặp marker do installer quản lý.
- `.codex/config.toml` đăng ký MCP `qiqi_delegate` cho Codex.
- `.mcp.json` đăng ký MCP `qiqi_delegate` cho Claude.
- `repos.yaml` liệt kê Git repository được phép nhận việc.
- `agent-routing.yaml` định nghĩa cách khởi động Peer.
- `.herdr-task-mcp/` giữ SQLite và trạng thái cục bộ.

Installer chỉ cập nhật vùng marker trong `AGENTS.md`. Installer kiểm tra cấu hình trước khi ghi. Không sửa các Git repository được giao việc.

Nếu cần Herdr session riêng, chạy:

~~~bash
.venv/bin/python -m qiqi_delegate.install \
  --workspace ../qiqi-control \
  --herdr-session dev-session
~~~

Nếu không đặt session riêng, runtime dùng socket hoặc session Herdr hiện có. Sau khi cài, mở lại phiên Codex hoặc Claude trong workspace để client nhận cấu hình MCP.

## Khai báo repository

Sửa `../qiqi-control/repos.yaml`:

~~~yaml
repositories:
  - name: backend
    path: ../backend
  - name: frontend
    path: ../frontend
~~~

`path` là đường dẫn tương đối so với workspace điều phối. Bạn có thể khai báo Git root nằm trong workspace hoặc nằm cạnh workspace.

Runtime từ chối repository khi:

- `path` không trỏ đúng Git root;
- đường dẫn vượt khỏi phạm vi thư mục cha của workspace;
- repository không tồn tại;
- hai tên trỏ cùng Git root, kể cả qua symlink.

**Không thêm đường dẫn repository tùy ý vào TaskPacket.** Lead phải dùng `name` có trong `repos.yaml`.

## Khai báo route

Sửa `../qiqi-control/agent-routing.yaml`:

~~~yaml
routes:
  codex-balanced:
    agent: codex
    args: []
  claude-balanced:
    agent: claude
    args: []
~~~

Trong lời gọi MCP, truyền **tên route** như `codex-balanced`. Không truyền `codex` thay cho tên route.

`args` chứa tham số CLI của agent. Runtime tự cấu hình native result hook. Không ghi đè thiết lập hook qua `args`.

**CAUTION:** Chỉ bật tùy chọn bỏ qua approval khi đã kiểm tra quyền ghi của Peer. Peer có thể sửa file trong Git root được giao.

## Bắt đầu giao việc

Gọi `workspace_info` trước. Công cụ trả tên repository, route và Herdr session đang dùng.

### Giao một TaskPacket

Gọi `delegate_repo_task`:

~~~json
{
  "repository": "backend",
  "route": "codex-balanced",
  "objective": "Phân tích luồng xử lý tạo đơn hàng",
  "scope": ["src/api", "tests"],
  "acceptance_criteria": [
    "Chỉ rõ entry point và luồng dữ liệu bằng file:line",
    "Nêu trường hợp lỗi và cách xác minh",
    "Cho biết test nào đã chạy và test nào chưa chạy"
  ],
  "constraints": ["Không sửa file"]
}
~~~

`TaskPacket` bắt buộc có `objective`, `scope` và `acceptance_criteria`. Có thể thêm `out_of_scope`, `constraints`, `context` và `known_unknowns`.

Trong `context`, tách `trusted_facts` khỏi `claims_to_investigate`. Mỗi mục có `source`.

Để RESUME, truyền `session_id` đã nhận từ lần chạy trước. Runtime chỉ chấp nhận session thuộc đúng repository và agent.

**Lead không đọc hoặc sửa repository đích thay Peer.** Peer cũng không được đọc repository khác. Nếu cần dữ liệu liên repository, Lead tạo TaskPacket riêng.

## Điều phối nhiều công việc bằng TaskGraph

`TaskGraph` là DAG gồm các node `repo_task`. Mỗi node khai báo repository, route, TaskPacket và các dependency.

Ví dụ payload của `start_graph`:

~~~json
{
  "graph": {
    "nodes": [
      {
        "node_id": "backend-contract",
        "repository": "backend",
        "route": "codex-balanced",
        "task_packet": {
          "objective": "Xác định contract đơn hàng",
          "scope": ["src/api"],
          "acceptance_criteria": ["Cung cấp contract và file:line"]
        }
      },
      {
        "node_id": "frontend-check",
        "repository": "frontend",
        "route": "claude-balanced",
        "depends_on": ["backend-contract"],
        "task_packet": {
          "objective": "Đối chiếu client với contract đã được Lead chấp nhận",
          "scope": ["src"],
          "acceptance_criteria": ["Nêu điểm tương thích và sai khác"]
        }
      }
    ]
  }
}
~~~

`start_graph` kiểm tra và lưu DAG, không khởi động Peer. Thực hiện theo thứ tự:

1. Gọi `start_graph`. Lưu `graph_run_id`.
2. Gọi `get_graph`. Đọc `revision` và `runnable_nodes`.
3. Gọi `delegate_next`. Runtime chạy một wave không xung đột.
4. Gọi `get_graph`. Lấy `review_required` và `attempt_id`.
5. Gọi `get_node_reviews` với đúng `node_id` và `attempt_id`.
6. Đối chiếu `agent_response` với từng tiêu chí chấp nhận.
7. Gọi `submit_decisions` kèm `expected_revision` hiện tại.
8. Lặp lại nếu TaskGraph còn việc.

`get_node_reviews` chấp nhận tối đa **8** review locator mỗi lần. `expected_revision` ngăn việc ghi quyết định trên snapshot cũ.

Node có dependency chỉ chạy khi Lead đã ACCEPT các node trước. Wave có thể chạy song song trên nhiều Git root không xung đột.

### Quyết định của Lead

| `action` | Kết quả | Trường bổ sung |
|---|---|---|
| `accept` | Chấp nhận evidence; node thành `satisfied` | Attempt phải `settled` và có captured response hợp lệ |
| `retry` | Đưa node về `pending` | `feedback` và `resume_session` nếu cần |
| `replan` | Chặn graph hiện tại để sửa topology | Bắt buộc `owner` và `return_checkpoint` |
| `block` | Chặn node đang thiếu điều kiện tiếp tục | Bắt buộc `owner` và `return_checkpoint` |

Gọi `reconcile_graph` cùng `expected_revision` để áp dụng TaskGraph mới. Runtime lưu lịch sử attempt và retry plan vào SQLite.

Trạng thái `settled` chỉ cho biết Peer đã trả kết quả. **Nó không tương đương ACCEPT.** Lead phải đọc captured evidence trước khi quyết định.

## Native capture và trạng thái lỗi

`qiqi_delegate.result_hook` ghi native Stop event của Codex hoặc Claude. Runtime dùng dữ liệu này làm nguồn kết quả. Không dùng terminal scraping để suy đoán final response.

Khi nhiều capture không xác định được một kết quả duy nhất, runtime trả `capture_ambiguous`. Không ACCEPT attempt này.

Lỗi MCP có thể chứa `repository_registry_invalid`, `routing_invalid`, `worker_runtime_failed` hoặc `agent_startup_blocked`. Đọc thông tin hành động kèm lỗi trước khi thử lại.

## Phục hồi khi worker bị gián đoạn

**CAUTION: Không xóa write claim khi worker còn chạy.** Hai writer có thể đồng thời ghi vào một Git root.

Runtime lưu write claim theo Git root chuẩn hóa trong `.herdr-task-mcp/qiqi_delegate.sqlite3`. Đổi tên repository trong `repos.yaml` không tạo quyền ghi thứ hai vào cùng Git root.

### Phục hồi write claim

1. Kiểm tra Herdr pane của worker.
2. Dừng worker và đóng workspace Herdr tương ứng.
3. Xác nhận worker không còn ghi vào Git root.
4. Đọc claim hiện tại:

~~~bash
.venv/bin/python -m qiqi_delegate.maintenance show-claim \
  --workspace ../qiqi-control --repository backend
~~~

5. Dùng **đúng** `claim_id` vừa đọc để giải phóng:

~~~bash
.venv/bin/python -m qiqi_delegate.maintenance release-claim \
  --workspace ../qiqi-control --repository backend \
  --claim-id 'turn:<exact-id>' --worker-termination-confirmed
~~~

Lệnh ghi audit trong SQLite. Khi database cũ chứa claim chưa xác định được Git root, runtime chặn delegation mới. Operator phải xác minh worker rồi phục hồi từng claim theo ID chính xác.

### Phục hồi TaskGraph attempt

Không tự phát lại attempt còn `running` sau restart.

Kiểm tra attempt:

~~~bash
.venv/bin/python -m qiqi_delegate.maintenance show-attempt \
  --workspace ../qiqi-control --repository backend \
  --graph-run-id '<graph-run-id>' --attempt-id '<attempt-id>'
~~~

Sau khi dừng worker và xử lý write claim liên quan, chạy:

~~~bash
.venv/bin/python -m qiqi_delegate.maintenance recover-attempt \
  --workspace ../qiqi-control --repository backend \
  --graph-run-id '<graph-run-id>' --wave-id '<wave-id>' \
  --node-id '<node-id>' --attempt-id '<attempt-id>' \
  --worker-termination-confirmed
~~~

Runtime kiểm tra toàn bộ ID, đánh dấu attempt `failed` và ghi audit. Wave chỉ đóng khi không còn attempt `running`. Lead cần review kết quả trước khi RETRY hoặc REPLAN.

`qiqi_delegate.maintenance` là CLI chỉ dành cho operator. Cờ `--worker-termination-confirmed` không tự xác minh tiến trình Herdr.

## Kiểm thử

Cài dependency cho test:

~~~bash
.venv/bin/python -m pip install '.[test]'
~~~

Chạy bộ test Python:

~~~bash
.venv/bin/python -m compileall -q qiqi_delegate
.venv/bin/python -m pytest -q tests-standalone
~~~

GitHub Actions chạy test trên Python **3.10, 3.12 và 3.13**. Workflow nằm tại `.github/workflows/qiqi-python.yml`.

## Cấu trúc mã nguồn

| Đường dẫn | Vai trò |
|---|---|
| `qiqi_delegate/install.py` | Tạo cấu hình MCP và quy tắc workspace |
| `qiqi_delegate/server.py` | Khai báo MCP tools |
| `qiqi_delegate/core.py` | Kiểm tra TaskPacket và phân tích native capture |
| `qiqi_delegate/result_hook.py` | Ghi sự kiện native Stop |
| `qiqi_delegate/runtime.py` | Quản lý Herdr, session và write claim |
| `qiqi_delegate/task_graph_validation.py` | Kiểm tra DAG và dependency |
| `qiqi_delegate/task_graph_scheduler.py` | Tính trạng thái và node có thể chạy |
| `qiqi_delegate/task_graph_runtime.py` | Điều phối wave và quyết định Lead |
| `qiqi_delegate/task_graph_store.py` | Lưu TaskGraph, attempt, retry plan và audit |
| `qiqi_delegate/maintenance.py` | Phục hồi có kiểm soát dành cho operator |
| `tests-standalone/` | Regression tests của runtime Python |

Không sửa trực tiếp dữ liệu SQLite để xử lý công việc kẹt. Dùng các lệnh bảo trì có kiểm tra ID và ghi audit.
