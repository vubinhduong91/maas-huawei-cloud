## Hub-fix Phase 0: lỗi 400 và mapping thinking/effort

Câu hỏi: gateway sửa body vì Claude Code (Sonnet) có 5 mức thinking còn GLM có 3, gửi mức GLM không hỗ trợ
thì MaaS trả 400. Chính xác trường nào, giá trị nào gây lỗi, và phải map thế nào?

Dữ liệu: ECS Singapore, Claude Code 2.1.296 headless, MaaS `glm-5.3` endpoint `ap-southeast-1`, ngày
2026-10-13. Chi tiết cách chạy và số liệu gốc: `ASSUMPTIONS.md`, các mục "Lab hub-fix — Phase 0".

- Bước 2 (`scenarios/hubfix_capture.sh`, `scenarios/hubfix_extract.py`): 17 lần chạy `claude -p`, bắt body
  ở tap-pre, gửi thẳng tới MaaS. Kết quả trên ECS: `reports/hubfix-phase0-capture.md`, `logs/hubfix/capture*.json*`.
- Bước 3 (`scenarios/hubfix_probe.py`): 23 variant, mỗi variant đổi một hoặc hai trường trên body thật rồi gửi
  thẳng tới MaaS. Kết quả trên ECS: `reports/hubfix-phase0-probe*.md`, `logs/hubfix/probe.jsonl` (body lỗi nguyên văn).

### 1. Claude Code gửi gì

| Mức chọn | glm-5.3 (`ANTHROPIC_MODEL`) | `--model sonnet` → `claude-sonnet-5-5` |
|---|---|---|
| không chọn (mặc định) | `output_config.effort = "high"` | `output_config.effort = "medium"` |
| `--effort low…max` | `output_config.effort` = đúng mức đó | như cột trái, **thêm** `messages[1].output_config.effort` cùng giá trị |

Giống nhau ở mọi case: `thinking = {"type":"adaptive","display":"omitted"}`,
`context_management = {"edits":[{"type":"clear_thinking_20251015","keep":"all"}]}`, header `anthropic-beta`
có `effort-2025-11-24`. Khác theo model: `max_tokens` (glm 32000, Sonnet 128000 rồi 64000 khi thử lại); với
Sonnet có thêm message `role: "system"` ở `messages[1]` (beta `mid-conversation-system-2026-04-07`,
`per-turn-control-2026-07-01`) mang `output_config` riêng. `CLAUDE_CODE_EFFORT_LEVEL=max` cho body giống
`--effort max`. `MAX_THINKING_TOKENS`, `CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1` không đổi gì trong body.

### 2. Bảng lỗi (MaaS `glm-5.3`)

| Trường | Giá trị gửi | HTTP | Message MaaS (nguyên văn) |
|---|---|---:|---|
| `output_config.effort` (top-level) | `low`, `high`, `max` | 200 | |
| | `medium`, `xhigh` | **400** | `ModelArts.81001` `request param validation error, Value error, reasoning_effort must be [low, high, max] for glm-5.3` |
| | không có `output_config` | 200 | (MaaS dùng mức mặc định của nó) |
| `messages[1].output_config.effort` | `medium` (top-level `high`) | 200 | MaaS không kiểm trường này |
| `model` | `claude-sonnet-5-5` | **404** | `ModelArts.81009` `Invalid model.` (kiểm trước effort: Sonnet + `medium` vẫn ra 404) |
| `thinking` | `adaptive` (có/không `display`), `enabled` + `budget_tokens` 16000 | 200 | |
| `max_tokens` | 32000, 64000, 128000 | 200 | |

Body lỗi có dạng `{"error":{"code","message","param","type":"BadRequest"},"error_code","error_msg","span_id"}`,
không theo dạng lỗi Anthropic (`{"type":"error","error":{"type":"invalid_request_error",...}}`).

Hệ quả: dev chọn Sonnet mà không chỉnh effort sẽ gửi `claude-sonnet-5-5` + `medium` → 404, và sau khi đổi
model thì vẫn → 400. Đó là đúng hai lỗi gateway phải sửa.

### 3. Mapping đã duyệt (2026-10-13)

Chỉ sửa **hai giá trị**, mọi thứ khác giữ nguyên byte:

| Trường | Giá trị vào | Giá trị ra |
|---|---|---|
| `model` (top-level) | `claude-sonnet-5-5` (tên Sonnet Claude Code gửi) | `glm-5.3` |
| `output_config.effort` (top-level) | `low` | `low` (không sửa) |
| | `medium` | **`high`** |
| | `high` | `high` (không sửa) |
| | `xhigh` | **`max`** |
| | `max` | `max` (không sửa) |
| | không có / giá trị khác | không sửa |

- Làm tròn **lên** để không giảm chất lượng coding (spec: không bỏ thinking, map về mức GLM gần nhất).
  Đổi lại, `medium` (mức mặc định của Sonnet, tức là phần lớn request) chạy ở `high`: tốn output token và
  chậm hơn. `xhigh → max` cũng nặng hơn `xhigh`.
- Mapping cố định: cùng giá trị vào luôn ra cùng giá trị, nên các request trong một session vẫn giống nhau
  ở chỗ đã sửa, prefix cache không bị ảnh hưởng.
- Không sửa: `thinking`, `max_tokens`, `context_management`, `messages[1].output_config` (MaaS không kiểm),
  header `anthropic-beta`.

### 4. Lưu ý cho Phase 1–2

- **Vị trí trong body** (body Claude Code là JSON compact, thứ tự top-level
  `model, messages, system, tools, metadata, max_tokens, thinking, context_management, safeguards, output_config, stream`):
  `"model"` là key đầu tiên (offset 1); `output_config` top-level nằm gần cuối, **sau** `tools`.
- **Key trùng tên ở chỗ khác**: trên 4.476 body có `output_config` trong `logs/raw/pre`, mọi body có 2 lần
  `"model"`; `"effort"` có 2 lần ở 4.461 body, 3 lần ở 14, 1 lần ở 1. Lần còn lại nằm trong `tools` (schema của tool có property `model`/`effort`).
  Body Sonnet còn có `"output_config":{"effort":...}` trong `messages[1]`, đứng **trước** cái top-level. Thay
  chuỗi lần đầu tìm thấy sẽ sửa sai chỗ; P4 phải xác định đúng key ở độ sâu 1.
- Gateway lab đang bật passthrough `claude-*` → Anthropic. Route P3/P4 phải tắt passthrough (hoặc map `model`
  trước khi định tuyến), nếu không request Sonnet sẽ không tới MaaS.

### 5. Cần xác minh

- Tên model dev thực sự gửi qua Hub (lab dùng mặc định của `--model sonnet` trên 2.1.296 = `claude-sonnet-5-5`).
  Có cần map cả `claude-opus-*`, `claude-haiku-*` (subagent, request phụ) không.
- MaaS có dùng `messages[1].output_config.effort` (per-turn) không, hay chỉ bỏ qua.
- Mức effort mặc định của MaaS khi không có `output_config`.
- Biến thể cấu hình Hub thật (A: một đoạn Lua decode → sửa → encode, hay B: `ai-proxy` + Lua).
