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
    args: ["--yolo"]
  claude-balanced:
    agent: claude
    args: ["--permission-mode", "auto"]
~~~

Trong lời gọi MCP, truyền **tên route** như `codex-balanced`. Không truyền `codex` thay cho tên route.

`args` chứa tham số CLI của agent. Runtime tự cấu hình native result hook. Không ghi đè thiết lập hook qua `args`.

**CAUTION:** `--yolo` cho phép Codex bỏ qua approval và sandbox thông thường. `--permission-mode auto` chọn chế độ quyền tự động của Claude. Chỉ sử dụng các mặc định này trong workspace và Git repository mà bạn tin cậy.

Muốn giữ chế độ yêu cầu xác nhận, sửa `args: []` cho từng route. Installer chỉ tạo `agent-routing.yaml` khi file chưa tồn tại; chạy lại installer không ghi đè cấu hình hiện có.

## Task Readiness & Discovery-on-demand (Issue #6)

TaskGraph hợp lệ không chứng minh task đúng ý user. Workflow mới giữ nguyên **user request** và context sources **tùy chọn**, dùng một Context Assessment đã lưu (với revision) để quyết định có thể giao ngay hay cần điều tra.

### Quy trình Lead

1. `prepare_task_request(user_request, sources?)` đăng ký yêu cầu. `sources=[]` hoàn toàn hợp lệ: **không cần spec hay handoff**. Server không truy cập được lịch sử chat; hãy truyền nguyên văn request.
2. `get_task_request(request_id)` trả user request, snapshots, provenance, revision, stale source IDs. Các nguồn hiện hỗ trợ `inline`, `repo_file`, `workspace_file` và captured `peer_turn`. Nguồn tài liệu trong Git root chỉ được đọc bằng locator chính xác; không mở quyền đọc repository tùy ý cho Lead.
3. `submit_context_assessment(request_id, expected_revision, assessment)` nhận requirements có `evidence_refs`, `blocking_unknowns`, `decision` và `rationale`. Requirement từ prompt gốc dẫn `request:current`; không cần source ngoài.
4. `direct` khi yêu cầu đủ actionable để giao Peer (Peer tự tìm chi tiết cục bộ). Gọi `start_graph(..., task_request_id, task_request_revision, requirement_map)` hoặc `delegate_repo_task(..., task_request_id, task_request_revision, requirement_refs)`. Graph mapping phải bao phủ mọi requirement.
5. `targeted_discovery` nếu thiếu một fact có thể đổi kế hoạch; `full_discovery` nếu chưa hiểu hệ thống. Gọi `delegate_discovery(request_id, repository_names, route, questions, mode)`. Một Peer có thể đọc các Git root đã đăng ký qua `--add-dir`. Peer result được capture/append vào request; **assessment cũ bị invalidated**, QiQi review và reassess trước khi triển khai.
6. `blocked` nếu thiếu quyết định nghiệp vụ mà đọc code cũng không giải quyết. Yêu cầu user làm rõ, không tự tưởng tượng.

### Ví dụ A: Yêu cầu rõ, không document → DIRECT

~~~json
{"user_request":"Trong backend, retry HTTP 429 và 503 tối đa 3 lần, giữ public API và viết regression tests"}
~~~

Không cần sources. Assessment sau `prepare_task_request` (giả sử `revision=1`):

~~~json
{"request_id":"<request_id>","expected_revision":1,
 "assessment":{
   "requirements":[
     {"id":"R1","text":"Retry 429/503 tối đa 3 lần","evidence_refs":["request:current"]},
     {"id":"R2","text":"Giữ public API và thêm tests","evidence_refs":["request:current"]}
   ],
   "blocking_unknowns":[],
   "decision":"direct",
   "rationale":"Mục tiêu, repository và acceptance conditions đã đủ để giao implementation"
 }}
~~~

Sau assessment dùng revision mới (ví dụ 2). Graph node `retry-backend` gắn `requirement_map={"retry-backend":["R1","R2"]}` và có TaskPacket tự đủ nghĩa. **Không gọi Discovery**.

### Ví dụ B: Chưa rõ nguyên nhân → FULL DISCOVERY

User: "Hệ thống đôi khi tạo trùng đơn hàng; tìm nguyên nhân và sửa." Không có handoff/spec.

QiQi đánh giá `decision="full_discovery"`, `blocking_unknowns=["Không biết luồng gây duplicate order"]`, rồi gọi:

~~~json
{"request_id":"<request_id>","repository_names":["backend","frontend"],
 "route":"codex-balanced","mode":"full_discovery",
 "questions":["Trace request/retry/idempotency xuyên backend/frontend có file:line",
              "Nêu nguyên nhân đã xác minh và những unknowns còn lại"]}
~~~

Sau captured response, QiQi tạo assessment mới, quyết định triển khai theo evidence thay vì phỏng đoán. Nếu chỉ thiếu một contract/idempotency detail, dùng `targeted_discovery`.

### Ví dụ C: Context inline → DIRECT

~~~json
{"user_request":"Implement theo spec được cung cấp, giữ API",
 "sources":[{"kind":"inline","label":"user-spec",
             "text":"Retry HTTP 503 only, max 2 attempts. No public API changes."}]}
~~~

Dùng `sources[0].id` trả về làm `evidence_refs`. `verification=reported` chỉ cho biết nội dung được **cung cấp**, không đồng nghĩa đã chứng minh trong code. Nếu đã đủ để giao Peer thì DIRECT.

### Ví dụ D: Handoff/spec chỉ là nguồn tùy chọn

~~~json
{"user_request":"Tiếp tục phần còn lại theo tài liệu",
 "sources":[{"kind":"repo_file","repository":"backend","path":"handoff.md"}]}
~~~

QiQi phân biệt phần `completed` và `remaining`, đối chiếu chỉ dẫn mới, chỉ Discovery khi thiếu thông tin ảnh hưởng kế hoạch. Runtime giữ snapshot/hash; source file thay đổi sau assessment có thể chặn dispatch.

### Ranh giới đảm bảo và legacy

- Các API cũ `start_graph`/`delegate_repo_task` không có `task_request_id` vẫn dùng chế độ **legacy_unassessed**, không được tuyên bố đã qua readiness gate.
- Bound TaskGraph sử dụng revision guard, kiểm tra blocking unknowns và ánh xạ node với requirements; prompt Peer nhận user request, các requirements liên quan và accepted upstream response.
- Discovery chỉ truyền những context source được `assessment.requirements[].evidence_refs` tham chiếu, không tự gửi mọi tài liệu đã nạp. Nếu cần một nguồn cho Discovery, đưa `source:id` đó vào assessment.
- Native captured Discovery/Peer evidence có thể dài tới giới hạn capture 256.000 ký tự và vẫn được lưu đầy đủ trong `get_task_request` (kể cả khi vượt giới hạn 100.000 byte dành cho nguồn inline/file). Khi chuyển evidence vào Implementation Peer, giới hạn `TaskPacket` 100.000 ký tự vẫn áp dụng: runtime trả lỗi có hướng xử lý, **không cắt ngầm** nội dung. Lead cần tạo nguồn ngắn hơn có dẫn xuất rõ ràng hoặc replan nếu packet quá lớn.
- Mỗi Task Request giới hạn 16 context sources. Discovery cần một slot trống cho kết quả: runtime **giữ chỗ trước khi chạy Peer** và không cho append khác chiếm vị trí đó. Nếu đã có 16 nguồn, Discovery bị từ chối trước khi dispatch; tạo request mới với các nguồn thực sự liên quan. Nếu kết quả native đã capture nhưng gắn nguồn không thành công (ví dụ revision race), response vẫn được trả kèm `attachment_error` và `turn_id` để có thể khôi phục ở request mới. Sau khi MCP process bị dừng đột ngột, lần khởi động tiếp theo sẽ tự nhận diện reservation `requested` của process đã chết và đánh dấu `interrupted` để giải phóng slot; không ảnh hưởng đến reservation của process còn sống. Nếu source đã được ghi vào SQLite trước khi crash (`attaching`), recovery đánh dấu `settled`, giữ nguyên source và `turn_id`. Nếu native capture đã được ghi vào bảng `turns` nhưng process chết **trước khi append source**, runtime vẫn có `turn_id` liên kết từ trước lúc chạy Peer; recovery tự đính kèm đầy đủ captured result, tăng revision và invalidates assessment để QiQi đánh giá lại. Chỉ khi chưa có native capture hợp lệ mới đánh dấu `interrupted`. Khi Discovery bị hủy sau native capture, bản ghi vẫn giữ `turn_id` đã bind để có thể xem và đính kèm lại evidence; cancellation không xóa turn đã capture. Khi startup recovery tự đính kèm một captured turn, revision và assessment có thể thay đổi: thao tác append/Discovery đang dùng revision cũ sẽ bị từ chối và Lead phải lấy context mới, đánh giá lại trước khi tiếp tục; runtime không cấp slot hoặc chuyển state dựa trên snapshot trước recovery. Trên Linux hệ thống dùng PID kèm thời điểm bắt đầu process để tránh nhầm PID được tái sử dụng; trên hệ điều hành thiếu `/proc` chỉ kiểm tra PID còn sống (best-effort).
- **Rolling upgrade / Discovery không có owner PID:** Khi khởi động MCP, migration của `task_discoveries` dùng SQLite `BEGIN IMMEDIATE` và đọc lại schema trước `ALTER TABLE`, tránh lỗi nhiều process nâng cấp đồng thời. Các reservation `requested`/`attaching` cũ với `owner_pid=NULL` **không tự động thu hồi**: worker của phiên bản MCP cũ có thể vẫn đang chạy, nên slot được giữ nguyên. Operator phải kiểm tra Herdr worker/process đã dừng trước khi khôi phục chính xác một `discovery_id` bằng lệnh:

  ```bash
  python -m qiqi_delegate.maintenance show-discovery --workspace /path/to/control-workspace --discovery-id UUID
  python -m qiqi_delegate.maintenance recover-ownerless-discovery --workspace /path/to/control-workspace --discovery-id UUID --worker-termination-confirmed
  ```

  Không đặt cờ xác nhận khi worker còn chạy. Lệnh không mở qua MCP Lead, chỉ xử lý record ownerless đúng ID và ghi audit. `show-discovery` chỉ đọc đúng record yêu cầu và **không chạy recovery toàn cục**; `recover-ownerless-discovery` chỉ được thay đổi đúng Discovery ID đã xác nhận. Cơ chế recovery tự động khi khởi động MCP server thông thường vẫn được duy trì. Nếu đã có native captured turn hợp lệ, recovery giữ nguyên full evidence và gắn vào context; nếu chưa, chuyển sang `interrupted` để giải phóng slot.
- **Discovery no-write chỉ bằng prompt.** Route Codex có thể giữ `--yolo`; `--add-dir` không phải sandbox read-only. Agent vẫn có thể ghi vào repo bổ sung dù instruction cấm, write claim không bao phủ hết các repo này. Chỉ sử dụng trên repository đáng tin cậy.
- Runtime chỉ kiểm tra cấu trúc, refs, digest và revision; không đảm bảo tuyệt đối suy luận ngữ nghĩa của QiQi. Cần execution trace thật để kiểm chứng LLM có tuân thủ.


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
