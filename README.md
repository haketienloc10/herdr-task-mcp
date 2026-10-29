# qiqi_delegate

`qiqi_delegate` là MCP server Python để giao việc cho Codex và Claude qua Herdr. **Lead** phân tích yêu cầu, giao `TaskPacket` và review kết quả của **Peer**. Runtime quản lý `TaskGraph`, SQLite và quyền ghi theo Git root. Không cần daemon điều phối riêng.

## Yêu cầu

- Python 3.10+, `pip` và `git`.
- Herdr CLI (`herdr`) trong `PATH`, hoặc cấu hình `QIQI_HERDR_BIN`.
- Codex hoặc Claude đã được cấu hình trong Herdr.
- Một workspace điều phối riêng và các Git repository nhận việc.

## Cài đặt

```bash
git clone https://github.com/haketienloc10/herdr-task-mcp.git
cd herdr-task-mcp
python3 -m venv .venv
.venv/bin/python -m pip install .

mkdir -p ../qiqi-control
.venv/bin/python -m qiqi_delegate.install --workspace ../qiqi-control
```

Installer cấu hình MCP trong workspace điều phối:

| File | Vai trò |
|---|---|
| `AGENTS.md`, `CLAUDE.md` | Quy tắc dành cho Lead |
| `.codex/config.toml`, `.mcp.json` | Cấu hình MCP cho Codex và Claude |
| `repos.yaml` | Danh sách repository được phép nhận việc |
| `agent-routing.yaml` | Route khởi động Peer |
| `.herdr-task-mcp/` | SQLite và trạng thái runtime |

Installer giữ nội dung ngoài vùng quản lý và không sửa repository nhận việc. Sau khi cài đặt, mở lại Codex hoặc Claude **trong workspace điều phối** để nạp cấu hình MCP.

Nếu cần Herdr session riêng, thêm `--herdr-session dev-session` vào lệnh installer.

## Cấu hình

Khai báo Git root trong `../qiqi-control/repos.yaml`:

```yaml
repositories:
  - name: backend
    path: ../backend
  - name: frontend
    path: ../frontend
```

`path` tính từ workspace điều phối. Runtime chỉ chấp nhận Git root tồn tại, thuộc phạm vi cho phép và không trùng nhau. `TaskPacket` phải tham chiếu `name` đã đăng ký.

Chọn route trong `../qiqi-control/agent-routing.yaml`. Installer mặc định tạo các route Claude Sonnet 5.5 theo mức effort: `claude-low`, `claude-balanced`, `claude-high`, `claude-xhigh`, `claude-max`. Route Codex được cấu hình riêng. Xem [`examples/agent-routing.claude.yaml`](examples/agent-routing.claude.yaml) để tùy chỉnh.

## Giao việc

Gọi `workspace_info` để kiểm tra repository và route. Ưu tiên workflow cấp cao:

1. `submit_workflow`: lưu `user_request`, đánh giá readiness và giao wave đầu.
2. `get_review_bundle`: đọc kết quả Peer và `revision` hiện hành.
3. `submit_decisions`: chọn `accept`, `retry`, `replan` hoặc `block`. Đặt `auto_continue=true` để chạy wave tiếp theo.

Ví dụ payload của `submit_workflow`:

```json
{
  "user_request": "Sửa retry HTTP 429 trong backend và thêm regression test",
  "assessment": {
    "requirements": [
      {
        "id": "R1",
        "text": "Retry HTTP 429 và giữ nguyên public API",
        "evidence_refs": ["request:current"]
      }
    ],
    "blocking_unknowns": [],
    "decision": "direct",
    "rationale": "Đã đủ yêu cầu để giao implementation"
  },
  "single_task": {
    "node_id": "retry",
    "repository": "backend",
    "route": "codex-balanced",
    "task_packet": {
      "objective": "Sửa retry HTTP 429, giữ nguyên public API",
      "scope": ["HTTP client", "tests"],
      "acceptance_criteria": ["Có regression test", "Không đổi public API"]
    }
  }
}
```

`TaskPacket` yêu cầu `objective`, `scope` và `acceptance_criteria`. Có thể thêm `constraints`, `out_of_scope`, `context` và `known_unknowns`.

- **`direct`:** Giao `single_task` hoặc `graph`. Graph nhiều node phải có `requirement_map` bao phủ mọi requirement. Chỉ đặt `depends_on` khi có dependency thực.
- **`targeted_discovery` / `full_discovery`:** Giao `discovery` khi thiếu dữ kiện ảnh hưởng kế hoạch. Sau khi đọc captured source, gọi `continue_workflow` trên cùng `request_id` với assessment mới.
- **`blocked`:** Dừng giao việc khi cần quyết định từ người dùng.

`sources` là tùy chọn. Có thể dùng `inline`, `repo_file`, `workspace_file` hoặc captured `peer_turn`. Không cần tài liệu ngoài nếu yêu cầu đã rõ.

## Review và hoàn tất

Lead phải đối chiếu captured evidence với từng `acceptance_criteria`. Trạng thái Peer `settled` **không có nghĩa là** `ACCEPT`.

| Quyết định | Ý nghĩa |
|---|---|
| `accept` | Chấp nhận kết quả của node |
| `retry` | Chạy lại với feedback |
| `replan` | Sửa kế hoạch qua `reconcile_graph` |
| `block` | Dừng node để xử lý điều kiện còn thiếu |

Gửi quyết định với `expected_revision` hiện hành. Node phụ thuộc chỉ chạy khi các node trước đã được ACCEPT. `TaskGraph` chuyển sang `complete` khi mọi node đều `satisfied`; không có bước finalize riêng.

Nếu `get_review_bundle` cắt ngắn response, dùng `get_review_evidence` với `node_id`, `attempt_id` và `next_offset`. Đọc đủ evidence trước khi ACCEPT. Nếu lỗi chỉ xảy ra khi tạo review bundle, **không gửi lại workflow hoặc quyết định đã lưu**.

## Giới hạn và phục hồi

- Discovery chỉ yêu cầu không ghi file qua prompt; **chưa có sandbox read-only bắt buộc**. Chỉ dùng với repository đáng tin cậy.
- `auto_dispatch` và `auto_continue` chờ Peer trong cùng MCP call; không chạy background job bền vững.
- Runtime ghi native capture, kiểm tra revision và giữ lịch sử trong SQLite. Không sửa SQLite trực tiếp.
- Sau `/clear`, dùng `get_graph_definition` để đọc TaskGraph gốc. Chỉ dùng `rebind_graph` khi đủ điều kiện kiểm tra nội dung và revision.

**CAUTION: Không giải phóng write claim khi worker còn chạy.** Hai worker có thể ghi đồng thời vào cùng Git root.

Trước khi phục hồi, kiểm tra Herdr pane, dừng worker và xác nhận worker không còn ghi. Sau đó đọc claim:

```bash
.venv/bin/python -m qiqi_delegate.maintenance show-claim \
  --workspace ../qiqi-control --repository backend
```

Chỉ giải phóng claim bằng đúng `claim_id` đã đọc, sau khi xác nhận worker đã dừng. Dùng `qiqi_delegate.maintenance` để phục hồi attempt hoặc Discovery bị gián đoạn; không chỉnh dữ liệu runtime bằng tay.

## Kiểm thử

```bash
.venv/bin/python -m pip install '.[test]'
.venv/bin/python -m compileall -q qiqi_delegate
.venv/bin/python -m pytest -q tests-standalone
```

CI chạy trên Python 3.10, 3.12 và 3.13 tại `.github/workflows/qiqi-python.yml`.
