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
├── CLAUDE.md
├── .codex/config.toml
├── .mcp.json
├── repos.yaml
├── agent-routing.yaml
└── .herdr-task-mcp/
    └── .gitignore
~~~

- `AGENTS.md` chứa quy tắc Lead dùng chung trong cặp marker do installer quản lý.
- `CLAUDE.md` import `@AGENTS.md` để Claude Lead dùng cùng quy tắc; installer giữ nguyên nội dung khác và không coi ví dụ trong fenced code (kể cả trong list), multiline inline-code spans hoặc HTML comments là import có hiệu lực. Nếu **managed block** nằm trong các ngữ cảnh này, hoặc một code span/fence/comment chưa đóng khiến import mới không thể kích hoạt, installer báo lỗi trước khi ghi file; hãy di chuyển/đóng khối Markdown rồi chạy lại.
- `.codex/config.toml` đăng ký MCP `qiqi_delegate` cho Codex.
- `.mcp.json` đăng ký MCP `qiqi_delegate` cho Claude.
- `repos.yaml` liệt kê Git repository được phép nhận việc.
- `agent-routing.yaml` định nghĩa cách khởi động Peer.
- `.herdr-task-mcp/` giữ SQLite và trạng thái cục bộ.

Installer chỉ cập nhật vùng marker trong `AGENTS.md` và `CLAUDE.md` (hoặc giữ nguyên import `@AGENTS.md` đã có). Để tránh công nhận nhầm import trong list/code, installer xác minh bằng parser **CommonMark** (`markdown-it-py`) cùng bộ kiểm tra fail-closed; khi hai cách kiểm tra không thống nhất, installer từ chối trước khi ghi thay vì coi import là hợp lệ. Đối với lỗi I/O thông thường, installer stage file thay thế và tạo hard-link inode cũ làm bản khôi phục **ngay trong thư mục đích tương ứng** trước khi ghi đè, để không làm lộ nội dung từ thư mục được bảo vệ ra workspace root. File thay thế phải sao chép và xác minh quyền, UID/GID và extended attributes (bao gồm POSIX ACL được lưu qua xattr); nếu không thể bảo toàn metadata thì từ chối trước khi ghi. Nếu commit một file sau đó thất bại, installer phục hồi các inode gốc. File mới được stage trong thư mục đích để kế thừa group, default ACL và quyền tạo file thông thường (0666 & umask), kể cả thư mục setgid; installer từ chối managed symlink, bao gồm `.herdr-task-mcp/.gitignore`. Không cam kết giao dịch xuyên qua mất điện, lỗi hệ thống hoặc ghi đồng thời từ tiến trình khác. Không sửa các Git repository được giao việc.

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

Sửa `../qiqi-control/agent-routing.yaml`, hoặc sao chép
[`examples/agent-routing.claude.yaml`](examples/agent-routing.claude.yaml).
Route hỗ trợ `agent`, `args` và `description` (không bắt buộc để giữ tương thích).

**Installer mặc định tạo 5 route Claude Sonnet 5.5**: `claude-low`,
`claude-balanced` (medium), `claude-high`, `claude-xhigh` và
`claude-max`. Mỗi bộ model + effort + permission chỉ có **một** route,
dùng chung cho mọi loại task có độ khó tương ứng. Mặc định
`--permission-mode auto`. Hai route Codex hiện có giữ riêng do
tham số permission khác nhau.

~~~yaml
routes:
  claude-balanced:
    agent: claude
    args: ["--model", "claude-sonnet-5-5", "--effort", "medium", "--permission-mode", "auto"]
    description: "Coding, bugfix, tài liệu hoặc test đơn giản khi yêu cầu rõ."
  claude-high:
    agent: claude
    args: ["--model", "claude-sonnet-5-5", "--effort", "high", "--permission-mode", "auto"]
    description: "Phân tích, planning, testing nhiều nhánh hoặc Final Evaluation."
~~~

`workspace_info()` trả `routes` (mapping name → agent như trước),
`route_names` và `route_details` gồm `description`, `model`,
`effort`, `args`. Lead chọn **mức effort thấp nhất đủ đáp ứng** độ khó,
rủi ro và bằng chứng cần có, không mặc định chọn mức cao nhất: effort tăng
có thể làm tăng thời gian và chi phí mà không giúp việc đơn giản.
`TaskPacket` vẫn quy định scope, quyền sửa và acceptance criteria.

**Không cần route `claude-evaluator` riêng**: sau khi TaskGraph hoàn tất,
gọi `start_final_evaluation` với `claude-high`, hoặc `claude-xhigh`
nếu cần đánh giá phức tạp. Vai trò Final Evaluator được quyết định bằng API
và TaskGraph context, không phải tên route. MCP không ép chính xác một profile
CLI nào: model, effort và permission do YAML quyết định. Nhưng evaluator
Claude **luôn bỏ project/local settings, .mcp.json** bằng
`--setting-sources user`, `--strict-mcp-config`, và chặn MCP/skills.
Native Stop/StopFailure hooks được cấp qua `--settings` chỉ để
capture kết quả. Không thêm override chính sách memory từ `--add-dir`;
user/managed settings do operator kiểm soát trong workflow đáng tin cậy.
Không đặt `--mcp-config` hay `--setting-sources` trong evaluator route;
các flags này thuộc ranh giới khởi động do runtime quản lý.
**Codex Evaluator** cũng bảo vệ thư mục khởi chạy trung lập: route cho
Final Evaluation không được dùng `-C`, `--cd` hoặc các dạng gộp
`-C/path`, `--cd=/path`, vì các cờ này có thể buộc Codex nạp
`AGENTS.md` của code ứng viên. Ngoài ra, **mọi Final Evaluation phải dùng phiên native mới ở host cục bộ**, không thể dùng
`resume`/`fork`/`queue`/`--remote` hoặc non-interactive `exec`/`e`/`review`
đối với Codex. Codex Final Evaluator chạy TUI tương tác từ neutral CWD;
không dùng `--skip-git-repo-check` (cờ chỉ dành cho `codex exec`), hoặc
`--resume`/`-r`, `--continue`/`-c`, `--fork-session`,
`--from-pr`, `--teleport`, `--session-id`, `attach`, `respawn`,
`--cloud`/`--remote`/`--environment` đối với Claude. Claude
Evaluator cũng chặn `-p`/`--print`, `--desktop`, `--bg`/`--background`,
`--init-only` và các tham số print-only `--max-turns`/`--max-budget-usd`.
Những tùy chọn này không tương thích với phiên tương tác foreground nhận
prompt và ghi kết quả bằng Stop hooks (bao gồm các dạng `--flag=value`).
Final Evaluation kiểm tra **cấu trúc và số lượng giá trị của từng CLI option**,
chỉ chấp nhận các options tương thích với phiên interactive foreground;
lệnh quản trị, non-interactive, positional prompt hoặc options chưa được
nhận diện sẽ bị từ chối **trước khi dispatch**. Giá trị của option hợp lệ
vẫn được giữ nguyên (ví dụ `--name attach` hoặc `--profile review`
không phải subcommand). Claude `--add-dir` có thể nhận nhiều thư mục;
`--restricted` của Claude, `--approve-for-me`/`--not-so-yolo`,
`--no-daemon`/`--strict-config` của Codex là cờ không có giá trị.
Claude Final Evaluator hỗ trợ `--ax-screen-reader` để hiển thị cho trình đọc màn hình.
Các tùy chọn đường dẫn `--system-prompt-file`, `--append-system-prompt-file`,
`--add-dir` (kể cả nhiều thư mục sau cùng một flag), `--plugin-dir` và
`--debug-file` nhận đường dẫn tương đối hoặc tuyệt đối. Đối với Final
Evaluation, đường dẫn tương đối được giải quyết từ workspace điều phối
(nơi chứa `agent-routing.yaml`), không phải `evaluator-neutral-cwd`.
Các đường dẫn tuyệt đối và route Peer thông thường được giữ nguyên;
native Claude vẫn kiểm tra path/type tại thời điểm khởi chạy.
Evaluator không chấp nhận `--permission-prompt-tool` vì tùy chọn đó chỉ
dành cho non-interactive mode.
Claude hỗ trợ `--advisor <model>` và `--debug` hoặc `--debug=filter`,
cùng các aliases
`-n`, `--allowed-tools`, `--disallowed-tools`. Các tùy chọn
`--allowedTools`/`--allowed-tools`, `--disallowedTools`/`--disallowed-tools`
nhận nhiều quy tắc sau một flag. Claude `--tools` và `--betas`
cũng nhận nhiều giá trị (ví dụ `--tools Read Glob`,
`--betas=beta-one beta-two`); cấu trúc giá trị được kiểm tra trước khi
khởi chạy evaluator, còn tên tool/beta được Claude xác thực khi chạy.
Codex `--image`/`-i` hỗ trợ nhiều
đường dẫn ảnh ngay cả khi ảnh đầu tiên gắn với flag (ví dụ
`--image=before.png after.png`, `-ibefore.png after.png`).
Cú pháp gán giá trị cho
zero-arity flag như `--search=false` sẽ bị từ chối. Model, effort, permission tiếp tục
do route YAML lựa chọn. `workspace_info().route_details.model` nhận
diện cả `--model` và native alias `-m` (bao gồm `-mVALUE`).
Route Peer thông thường không bị giới hạn bởi bộ kiểm tra này.

Lưu ý: `--add-dir` cung cấp quyền truy cập Git root trực tiếp,
không áp đặt filesystem chỉ đọc. Final Evaluator không tạo repository copy.
Prompt cấm thay đổi source, nhưng route permissive vẫn có thể cho phép
native agent thực hiện side effects. Multi-root claims ngăn Peer hợp tác
ghi vào các Git root trong lúc review; external writers không bị chặn.
Git fingerprint kiểm tra thay đổi trước/sau, không bảo đảm bất biến tại
mọi thời điểm. Cần OS sandbox hoặc immutable filesystem view nếu review
code không đáng tin cậy.

Runtime sở hữu native Stop/StopFailure hooks; không thêm `--settings` hoặc
`hooks.*` (kể cả cú pháp `--settings=...`, `--config=hooks.*`)
vào `args`. Claude Final Evaluation từ chối `--safe-mode` vì chế độ này
vô hiệu hóa native hooks cần thiết để capture kết quả. Route Peer thông thường
vẫn có thể sử dụng `--safe-mode`. Installer **không ghi đè** `agent-routing.yaml`
đã tồn tại; workspace cũ cần tự cập nhật cấu hình.

## Workflow mới: Lead → MCP → Peer (server-owned orchestration)

Các high-level MCP tools **gộp việc điều phối**, không thay thế Discovery, analysis,
TaskGraph, native capture, semantic review, retry/replan, write claim, readiness gate
hoặc independent final evaluation. Các tool thấp hơn vẫn có thể dùng để khôi phục
hoặc chẩn đoán.

- `submit_workflow`: nhận nguyên văn `user_request`, nguồn tùy chọn, `assessment` và
  đúng một `single_task` hoặc `graph` nếu `decision=direct`. Server tạo request +
  snapshot + assessment **nguyên tử**, validate mapping và preflight TaskPacket trước
  khi tạo graph, sau đó mặc định chạy wave đầu. `single_task` được ánh xạ tự động
  tới tất cả requirement IDs; multi-node graph cần `requirement_map`.
- `submit_workflow` cũng nhận `discovery` khi decision là `targeted_discovery` hoặc
  `full_discovery`; server vẫn gọi pipeline Discovery hiện hữu. Kết quả được capture
  và gắn source, assessment cũ bị invalidated. Lead **phải review và reassess**
  bằng `continue_workflow` trên **chính request_id đã có** để không bỏ provenance
  trước khi giao implementation. `blocked` không dispatch Peer.
- `get_review_bundle`: trả graph state cùng chính xác attempt hiện hành;
  **mặc định tổng JSON response tối đa 32.000 ký tự** (`max_total_chars`, điều
  chỉnh trong 1.024–64.000). Ngân sách bao gồm graph/review metadata và escaped
  evidence; phân bổ linh hoạt cho tối đa **8 review/trang** (mặc định). Nếu có
  một response Peer dài 12.000 ký tự và tổng JSON vừa ngân sách, trả trọn trong
  **một call**. `max_chars_per_review` vẫn nhận 1–64.000 như trần tùy chọn cho
  từng review; giá trị này không vượt được ngân sách tổng.
  Khi `response_truncated=true`, dùng `get_review_evidence` với
  `node_id + attempt_id + next_offset` (`max_chars` tối đa 24.000) để đọc phần
  còn lại; evidence gốc không bị cắt hoặc sửa trong storage. Nếu số review hoặc
  metadata không vừa một response, dùng `review_offset`/`next_review_offset`
  để lấy trang tiếp theo và **phải truyền** `expected_revision` từ trang đầu.
  Graph thay đổi giữa các trang sẽ trả stale revision, tránh bỏ sót attempt.
  Không ACCEPT chỉ dựa trên preview.
  Nếu chính graph/review metadata đã lớn hơn `max_total_chars`, standalone
  `get_review_bundle` từ chối thay vì tự vượt giới hạn.
  **Riêng sau khi high-level workflow đã persist thành công** (`submit_workflow`,
  `continue_workflow` hoặc `submit_decisions(auto_continue=true)`), lỗi dựng
  review bundle **không làm mất kết quả đã commit**: response vẫn có
  `graph_run_id`, `state`/`decision_outcomes`, `review_bundle_error`, và
  `review_bundle.incomplete=true` với `revision`, `review_required_count`,
  tối đa 8 exact `review_locators` (`node_id`, `attempt_id`) để tiếp tục
  đọc bằng `get_review_evidence`. Có thể thử
  `get_review_bundle(max_total_chars=64000)` cho metadata vừa ngân sách.
  **Không gửi lại workflow, cũng không gửi lại semantic decisions** chỉ vì
  review presentation failed; Peer và quyết định đã được persist.
- `submit_decisions(auto_continue=true)`: persist semantic decisions trước, rồi
  tự gọi wave tiếp nếu có runnable node. Chỉ node có tất cả dependency ACCEPT mới
  runnable; node độc lập có thể chạy dù node khác đang `awaiting_review`,
  **trừ khi bất kỳ node nào đã blocked/replanned**: trường hợp này phải
  giải quyết/reconcile graph trước dispatch. Nếu
  continuation lỗi, quyết định đã được commit và phản hồi có `continuation_error`.
  Nếu dispatch đầu tiên lỗi, `submit_workflow` vẫn trả `graph_run_id` cùng
  `dispatch_error` và review state để Lead phục hồi, không tạo graph trùng.
- API cũ `prepare_task_request`, `add_task_source`,
  `submit_context_assessment` và `get_task_request` **giữ nguyên full-content
  response mặc định** để tương thích Lead đã cài đặt, kể cả transport source
  có optional fields bằng `null`. Dùng `get_task_request_summary` (hoặc
  `get_task_request(include_content=false)`) cho metadata compact, và
  `get_task_source` để đọc content theo từng phần. Tool workflow mới tiếp tục
  dùng discriminated source schema và compact response.

### Direct: một call để đăng ký và chạy wave đầu

~~~json
{
  "user_request": "Điều tra và sửa retry 429 trong backend, thêm regression test",
  "sources": [
    {"alias": "spec", "source": {"kind": "inline", "text": "Giữ nguyên public API"}}
  ],
  "assessment": {
    "requirements": [
      {"id": "R1", "text": "Retry HTTP 429 và giữ public API",
       "evidence_refs": ["request:current", "alias:spec"]}
    ],
    "blocking_unknowns": [],
    "decision": "direct",
    "rationale": "Yêu cầu đã đủ actionable; chi tiết code do Peer tự điều tra"
  },
  "single_task": {
    "node_id": "retry",
    "repository": "backend",
    "route": "codex-balanced",
    "task_packet": {
      "objective": "Triển khai retry 429, giữ public API",
      "scope": ["HTTP client và regression tests"],
      "acceptance_criteria": ["Có regression test", "Không đổi public API"]
    }
  }
}
~~~

Server trả `task_request` compact, `graph_run_id` và `review_bundle`.
Lead review response của Peer, đọc thêm bằng `get_review_evidence` nếu dài,
rồi gọi `submit_decisions(graph_run_id, decisions, expected_revision,
auto_continue=true)`. Sau khi graph complete, thực hiện **final evaluation**
và `finalize_graph` như quy trình hiện tại; việc ACCEPT các node không thay thế
independent evaluation.

### Discovery và analysis vẫn được giữ nguyên

Khi `decision=targeted_discovery/full_discovery`, thay `single_task` bằng:

~~~json
"discovery": {
  "repository_names": ["backend", "frontend"],
  "route": "codex-balanced",
  "questions": ["Tìm retry origin với file:line", "Đối chiếu contract"],
  "mode": "targeted_discovery"
}
~~~

Lead đọc `captured_source_id` qua `get_task_source`, rồi gọi
`continue_workflow(request_id, expected_revision, assessment, single_task/graph)`
để lưu assessment mới và dispatch wave đầu trong một call. Dùng source ID đã
được capture trong `assessment.requirements[].evidence_refs`; không tạo một
Task Request mới hoặc sao chép evidence không cần thiết. Nếu vẫn thiếu fact,
`continue_workflow` cũng chấp nhận một `discovery` khác với assessment phù hợp.
Không đổi chính sách
Discovery chỉ đọc bằng prompt (chưa có sandbox enforce). Analysis đã actionable
vẫn dùng `decision=direct` với `single_task` hoặc `graph`, TaskPacket nêu rõ
phạm vi chỉ đọc và acceptance criteria về evidence.

**Giới hạn hiện tại:** `auto_dispatch`/`auto_continue` vẫn chờ Peer hoàn tất
trong cùng MCP call. Chưa bổ sung durable background job hoặc tự động retry
đối với Herdr startup/trust. Source đầy đủ vẫn được giữ trong SQLite;
preflight kiểm tra context hiện có trước tạo graph, upstream evidence mới phát
sinh được kiểm tra khi dispatch. Bản refactor không tự động rút gọn evidence.

## Task Readiness & Discovery-on-demand (Issue #6)

TaskGraph hợp lệ không chứng minh task đúng ý user. Workflow mới giữ nguyên **user request** và context sources **tùy chọn**, dùng một Context Assessment đã lưu (với revision) để quyết định có thể giao ngay hay cần điều tra.

### Quy trình Lead

1. `prepare_task_request(user_request, sources?)` đăng ký yêu cầu. `sources=[]` hoàn toàn hợp lệ: **không cần spec hay handoff**. Server không truy cập được lịch sử chat; hãy truyền nguyên văn request.
2. `get_task_request(request_id)` trả user request, snapshots, provenance, revision, stale source IDs. Các nguồn hiện hỗ trợ `inline`, `repo_file`, `workspace_file` và captured `peer_turn`. Nguồn tài liệu trong Git root chỉ được đọc bằng locator chính xác; không mở quyền đọc repository tùy ý cho Lead.
3. `submit_context_assessment(request_id, expected_revision, assessment)` nhận requirements có `evidence_refs`, `blocking_unknowns`, `decision` và `rationale`. Requirement từ prompt gốc dẫn `request:current`; không cần source ngoài.
4. `direct` khi yêu cầu đủ actionable để giao Peer (Peer tự tìm chi tiết cục bộ). Gọi `start_graph(..., task_request_id, task_request_revision, requirement_map)` hoặc `delegate_repo_task(..., task_request_id, task_request_revision, requirement_refs)`. Graph mapping phải bao phủ mọi requirement.
5. `targeted_discovery` nếu thiếu một fact có thể đổi kế hoạch; `full_discovery` nếu chưa hiểu hệ thống. Gọi `delegate_discovery(request_id, repository_names, route, questions, mode)`. Một Peer có thể đọc các Git root đã đăng ký qua `--add-dir`. Peer result được capture/append vào request; **assessment cũ bị invalidated**, QiQi review và reassess trước khi triển khai.
6. `blocked` nếu thiếu quyết định nghiệp vụ mà đọc code cũng không giải quyết. Yêu cầu user làm rõ, không tự tưởng tượng.

### Ví dụ A: Yêu cầu rõ, không document → readiness `direct`

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

Sau assessment dùng revision mới (ví dụ 2). Retry là một hành vi thống nhất nên có thể chọn `SINGLE_TASK`. Graph node `retry-backend` gắn `requirement_map={"retry-backend":["R1","R2"]}`. **Không gọi Discovery**.

Nếu yêu cầu gồm `Create Order` và `Cancel Order` độc lập, readiness vẫn là `direct`. Lead phải chọn `MULTI_TASK` và tạo hai node có acceptance criteria riêng.

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

### Ví dụ C: Context inline → readiness `direct`

~~~json
{"user_request":"Implement theo spec được cung cấp, giữ API",
 "sources":[{"kind":"inline","label":"user-spec",
             "text":"Retry HTTP 503 only, max 2 attempts. No public API changes."}]}
~~~

Dùng `sources[0].id` trả về làm `evidence_refs`. `verification=reported` chỉ cho biết nội dung được **cung cấp**, không đồng nghĩa đã chứng minh trong code. Nếu đã đủ điều kiện giao Peer thì assessment là `direct`. Lead vẫn phải chọn số task riêng.

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
- `workspace_file` là **file reference**, không đưa nội dung vào SQLite hoặc `TaskPacket`. Runtime lưu đường dẫn tương đối, dung lượng và SHA256; khi giao việc, runtime sao chép **chỉ những source đã được requirement tham chiếu** vào thư mục tạm độc lập, xác minh SHA256, cấp `--add-dir` cho Peer và gửi đường dẫn snapshot. Peer đọc khi cần. Snapshot đặt quyền 0444/0555 nhưng các quyền này **không tạo sandbox bảo mật** đối với agent chạy cùng UID hoặc với route `--yolo`. Snapshot bị xóa khi dispatch kết thúc. File UTF-8 tối đa 64 MiB; file thay đổi sau khi đăng ký sẽ chặn giao việc. `get_task_source` không đọc nội dung của `workspace_file` reference-only. `repo_file` và `inline` vẫn chuyển nội dung theo giới hạn cũ. Final Evaluator cũng sao chép và xác minh các source này vào snapshot riêng.
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

### Chính sách giao việc theo loại nhiệm vụ

Giữ **một schema `TaskPacket`** cho analysis, coding, testing và documentation. Lead chọn nội dung `objective`, `scope`, `acceptance_criteria` và `constraints` theo mục tiêu công việc; không cần thêm `task_type`, schema riêng hay node kind riêng. Với task kết hợp, chỉ ghép các tiêu chí thực sự liên quan.

| Loại việc | Gợi ý `acceptance_criteria` và `constraints` |
|---|---|
| Analysis / Discovery / Review | Chỉ yêu cầu nội dung liên quan đến mục tiêu: hành vi, luồng dữ liệu, contract, lỗi, giới hạn hay bằng chứng `file:line` khi áp dụng. Với task chỉ đọc, đặt `constraints` cấm sửa file. |
| Coding | Mô tả hành vi phải triển khai, phạm vi thay đổi, contract cần giữ, test liên quan; phân biệt test đã chạy và chưa chạy. |
| Testing | Nêu test cases, trường hợp biên, kết quả pass/fail và test chưa chạy. Xác định rõ có được thêm/sửa test và chạy lệnh kiểm thử hay không. |
| Documentation | Đối chiếu implementation/contract thực tế; yêu cầu tài liệu và ví dụ chính xác, không mô tả tính năng chưa có. |

Với task đơn giản như xác nhận vị trí một khóa cấu hình, `acceptance_criteria` chỉ cần yêu cầu đường dẫn `path:line` và giá trị được xác minh; không cần phân tích toàn bộ execution flow, contract hay các nhánh lỗi không liên quan.

Ví dụ phần `TaskPacket` cho coding (vẫn dùng `delegate_repo_task` hoặc `task_packet` trong graph như trước):

~~~json
{
  "objective": "Implement retry HTTP 503 tối đa hai lần",
  "scope": ["src/client", "tests"],
  "constraints": ["Không thay đổi public API", "Không retry lỗi ngoài HTTP 503"],
  "acceptance_criteria": [
    "Hành vi retry đáp ứng yêu cầu và giữ nguyên public API",
    "Có test cho HTTP 503 và lỗi không được retry",
    "Nêu file đã thay đổi, test đã chạy và test chưa chạy"
  ]
}
~~~

Không áp checklist đầy đủ cho mọi task. Lead đánh giá theo `acceptance_criteria` đã giao và evidence thực tế; trạng thái Peer `settled` không đồng nghĩa với `ACCEPT`.

### Quy tắc phân rã nhiệm vụ

Trước khi giao coding, Lead chọn `SINGLE_TASK` (một Peer) hoặc `MULTI_TASK` (nhiều task). Readiness `direct` chỉ cho phép giao việc, không quyết định số task.

- **Coding:** Bắt buộc `MULTI_TASK` khi có từ hai hành vi có thể ACCEPT riêng, checkpoint upstream hoặc retry chung gây lặp đáng kể công việc độc lập. Chọn `SINGLE_TASK` cho một kết quả gắn chặt, không cần checkpoint.
- **Analysis:** Chỉ chia câu hỏi hoặc giả thuyết độc lập. Giữ cùng Peer để truy vết một flow trong cùng Git root.
- **Discovery:** Chỉ gọi `delegate_discovery` khi assessment là `targeted_discovery` hoặc `full_discovery` và có blocker. Một Peer có thể điều tra nhiều Git root. Sau đó đánh giá lại readiness.
- **Nhiều Git root:** Mọi `repo_task` chỉ dùng một Git root. Task ghi nhiều repository, kể cả testing-only và documentation-only, phải tách theo Git root. Với analysis đã `direct`, giao từng root rồi Lead tổng hợp evidence. Nếu thiếu fact xuyên root ảnh hưởng kế hoạch, chọn Discovery trước khi giao việc.
- **Khi chia:** Mỗi task có `objective`, `scope` và `acceptance_criteria`. Chỉ đặt `depends_on` khi có dependency thực. Lead review và REPLAN khi dependency thay đổi.

Không chia theo module, layer hoặc file chỉ để tăng số task. Giữ code và test liên quan trong cùng task. Review, testing-only và documentation-only không bắt buộc phân rã theo kết quả chức năng; giới hạn Git root vẫn áp dụng.

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

## Khôi phục TaskGraph sau `/clear` và rebind an toàn

- `get_graph_definition(graph_run_id)` trả **toàn bộ authored DAG** từ
  `graph_runs.graph_json`: `graph.nodes[]` gồm `objective`, `scope`,
  `acceptance_criteria`, `constraints`, `context` (nếu có),
  `depends_on`, `repository`, `route`, kèm graph `revision` và
  `task_request_binding`. Graph definition và binding được đọc
  bằng **một SQLite snapshot** để tránh ghép TaskPacket cũ với requirement_map
  mới khi `reconcile_graph` diễn ra đồng thời. Tool chỉ đọc, không gọi Peer,
  không sửa node. Không dựng lại TaskPacket từ review preview hoặc suy luận
  thứ tự chạy.

- Khi bind graph mới (hoặc bind lại sau `reconcile_graph`), runtime ghi
  fingerprint của **toàn bộ** nội dung user request, context sources và
  assessment vào `task_graph_binding_snapshots`. Bảng này không làm thay đổi
  dữ liệu của các graph cũ.

- `rebind_graph(graph_run_id, expected_graph_revision, expected_request_revision)`
  là recovery **fail-closed**, *không* thay DAG, TaskPacket, node ACCEPT, attempt
  hay graph revision; không tự dispatch. Tool chỉ cập nhật
  `task_graph_bindings.request_revision` và ghi audit trong một SQLite
  transaction `BEGIN IMMEDIATE` nếu tất cả điều kiện cùng đúng:
  1. Graph hoàn tất, mọi node `satisfied` và có captured turn, không còn
     wave/attempt chạy.
  2. `expected_graph_revision` và `expected_request_revision` còn hiện hành.
  3. Request vẫn `direct`, không có blocking unknown hoặc source stale.
     Để loại bỏ race với tiến trình ghi file ngoài SQLite, safe rebind **từ
     chối mọi request có source `repo_file` hoặc `workspace_file`**, kể cả
     khi hash hiện tại còn khớp. Cần snapshot bất biến có chứng cứ để hỗ trợ
     trường hợp đó trong tương lai.
  4. Bản request hiện tại có **cùng digest chính xác** với nội dung đã
     snapshot lúc bind; toàn bộ requirement_map vẫn phủ đúng requirements.
  5. Graph persisted còn đầy đủ và fingerprint khớp; baseline snapshot
     của binding đã tồn tại.

  Ví dụ, nộp lại đúng y nguyên assessment chỉ làm tăng revision có thể được
  rebind an toàn **nếu request không chứa nguồn file mutable**. **Thêm R1–R9, sửa requirement, thay source, thay rationale
  hoặc source đã drift** không được coi là tương đương tự động: tool từ chối.
  Khi ấy phải review thay đổi và dùng `reconcile_graph` phù hợp, hoặc nâng cấp
  quy trình amendment có chứng cứ; tuyệt đối không `UPDATE` revision trực tiếp.

  Các graph được tạo **trước bản vá**, đã bị stale, không có snapshot baseline:
  `rebind_graph` sẽ từ chối thay vì tự chấp thuận bằng revision/digest mới.
  Vẫn có thể đọc packet gốc qua `get_graph_definition`, kiểm tra thủ công
  và chọn workflow recovery riêng theo bằng chứng. Không tuyên bố đã bảo toàn
  ACCEPT cho graph cũ khi thiếu proof.

  Sau rebind thành công, phải chạy Final Evaluation mới (hoặc xác minh PASS
  mới đã được lưu hợp lệ), rồi mới `finalize_graph`. PASS của request revision
  cũ không tự trở thành PASS cho revision mới.

## Final Evaluation Gate — Native Multi-Root Review

TaskGraph `complete` chỉ xác nhận execution hoàn thành. Mọi node phải `satisfied` với native Peer evidence trước khi có thể chạy Final Evaluation. **Lead/Peer ACCEPT không phải chứng cứ Final PASS.**

1. Bind verbatim Task Request, assessment và `requirement_map` khi gọi `start_graph`. TaskGraph không được bind không đủ điều kiện Final PASS.
2. Kết thúc các node bằng vòng `get_node_reviews` / `submit_decisions` hiện hành. Nếu có node cancelled/blocked, Final Evaluation từ chối.
3. Gọi `start_final_evaluation(graph_run_id, route, expected_revision)`. Route `claude-high`, `claude-xhigh` hoặc Codex tùy theo `agent-routing.yaml`. Mỗi lượt sử dụng **một native session mới**, bắt đầu từ `~/.qiqi/evaluator-cwd/runs/turn-<uuid>`, không tái sử dụng phiên Lead/Peer. Trust được thiết lập một lần tại thư mục cha cho các adapter hỗ trợ kế thừa trust.
4. Runtime xác thực Git roots thuộc TaskGraph trong `repos.yaml`, tạo **Git fingerprint** bằng diff/index, metadata quyền/loại của tracked files và nội dung non-ignored untracked files để phát hiện thay đổi. Không tạo manifest SHA256 từng file, không copy source, không từ chối symlink Git thông thường. Một transaction cấp claim cho **tất cả** Git root; native agent đọc source trực tiếp qua `--add-dir`.
5. Trong CWD trung lập, runtime tạo `context/context.json`, `context/source_index.json` và file nguồn Task Request được tham chiếu. Context chứa verbatim user request, requirements, requirement mapping, graph objective/acceptance criteria, lịch sử node attempts, Lead decisions, Git root fingerprints. Tất cả Lead/Peer reports chỉ là claims cần xác minh. Nguồn đính kèm `workspace_file` có thể được copy riêng lẻ để bảo toàn nội dung; **không có repository snapshot/copy**.
6. Evaluator phải review implementation thực tế ở **mọi Git root thuộc TaskGraph**, gồm mọi module liên quan, contract nội repository lẫn liên repository. Phải đối chiếu request, requirements, node evidence, producer/consumer interface, schema và data flow. Report gồm `verdict`, `requirement_results`, `cross_repository_checks`, `verification_runs`, `findings`, `unknowns`. Evidence yêu cầu `{repository,path,locator}`; deletion yêu cầu `{kind:"deleted",repository,path,locator}` nằm trong danh sách tracked deletions từ Git.
7. Native captured PASS **chưa phải Final PASS**. `FinalEvaluationStore.validate_report()` kiểm tra schema, coverage, repository scope và evidence locators (không tự chứng minh từng locator trỏ tới nội dung đã đọc). Kết quả phải thuộc native turn đã settled. Git fingerprint được so sánh sau native review và khi finalize; source thay đổi hoặc worker cleanup chưa xác nhận đều không được PASS.
8. Dùng `get_final_evaluation(graph_run_id, evaluation_id?)` để đọc evidence. Nếu FAIL/INCONCLUSIVE, Lead sửa hoặc reconcile rồi đánh giá lại toàn bộ graph. Chỉ gọi `finalize_graph(graph_run_id, evaluation_id, expected_revision)` khi PASS vẫn current. Các PASS cũ (policy v1/v2) lưu để audit nhưng không được tái sử dụng với policy v3.

**Concurrency/recovery:** Runtime giữ lock cho tất cả registered roots trong một transaction; chỉ nhả sau khi Herdr workspace đã đóng. Nếu native worker vẫn có thể tồn tại, giữ claims và evaluator CWD để operator kiểm tra. Phải xác nhận worker termination, release từng exact Git root claim, sau đó dùng `recover-final-evaluation`; capture đơn lẻ không thể phục hồi thành PASS.

**Giới hạn bảo mật và tính nhất quán:** `--add-dir` không phải filesystem sandbox. Evaluator có thể nhìn thấy file được Git ignore, symlink và nội dung ngoài Git root nếu native permissions cho phép; operator phải chạy trên workspace đáng tin cậy. Git fingerprint cũng duyệt **submodule worktrees theo gitlinks** bằng cùng phép tính Git diff/index/untracked; submodule chưa checkout bị từ chối thay vì công nhận source chưa đọc. Để tránh PASS sai, V3 từ chối tracked paths có Git check-in transformations (`filter`, `text`, `eol`, `ident`, `working-tree-encoding`, `crlf`) và root đặt `core.autocrlf`, `core.fileMode=false` hoặc `core.symlinks=false`, vì Git diff có thể che thay đổi nội dung, mode hoặc symlink. Untracked embedded Git repositories cũng bị từ chối thay vì chỉ hash metadata thư mục. Trước khi chạy Git worktree diff, Final Evaluation kiểm tra các tracked-path attributes và từ chối check-in conversions có thể chạy clean filter tùy ý. Các lệnh Git thu thập fingerprint luôn dùng `-c core.fsmonitor=false -c core.trustctime=true` để không tin báo cáo “clean” từ FSMonitor hook hoặc bỏ qua ctime; cấu hình Git trong repository không bị thay đổi. Git fingerprint là **best-effort freshness check**, không phải immutable snapshot hay chứng cứ từng file: dùng Git diff/index, chỉ đọc metadata mode/loại của tracked files (không hash nội dung của từng tracked file) và hash non-ignored untracked files; không bao phủ nội dung ignored files hay thay đổi thoáng qua rồi hoàn nguyên. Ghi source bị cấm trong prompt nhưng permission thực tế vẫn phụ thuộc `agent-routing.yaml`. Write claims chặn QiQi writers hợp tác, không chặn process bên ngoài. Nếu cần bảo đảm read-only, dùng filesystem sandbox bên ngoài. Report từ LLM là đánh giá độc lập, không phải chứng minh máy móc rằng từng locator đã được đọc.

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
| `qiqi_delegate/install.py` | Tạo cấu hình MCP và quy tắc Lead dùng chung cho Claude/Codex |
| `qiqi_delegate/evaluator_routes.py` | Profile CLI an toàn cho Codex/Claude Final Evaluator |
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


### Direct-root Final Evaluation — ignored-file security boundary

Native `--add-dir` gives the evaluator access to **all files** in each
registered Git root, including Git-ignored credentials (`.env`,
`private/.env`, `.aws/`, etc.) and potentially sensitive `.git`
metadata. **Final Evaluation v3 does not inspect or block ignored secrets
before launching**. The legacy `inspect_roots()` helper still has ignored-path
checks, but it is not used by the current coordinator. The native reviewer
must run only on trusted repositories without secrets that it should not see,
or inside an externally configured read-only, access-restricted OS sandbox.
The evaluator prompt alone is not a security boundary. There is no guarantee
that Git-ignored credentials are hidden from the native process.

### Codex follow-up — stable evaluator claim identity

When a native Evaluator starts and becomes blocked, recovery output uses
`release-claim --claim-repository <persisted-key> --claim-id <turn-id>`
for **each** held Git root. The persisted key identifies the immutable
`write_claims.repository` primary key, **not** the current `repos.yaml`
alias. The operator must still verify native worker termination and pass
`--worker-termination-confirmed`; only a matching exact row/claim pair
is released and audited. The prior `--repository` selector is supported
for callers working with currently registered Git root aliases.

The legacy `inspect_roots()` helper can detect ignored credential
paths such as `private/.env`, but the native v3 review path uses
`review_scope()` and does **not** run that preflight.
