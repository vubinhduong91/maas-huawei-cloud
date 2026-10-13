# Sửa gateway: giữ fix lỗi 400 mà không làm hỏng prompt cache

**Khuyến nghị: thay cách gateway sửa body bằng P4: chỉ thay đúng byte của `model` và `output_config.effort`,
không encode lại cả body.** P4 đạt cả 4 mục tiêu:
- 0 lỗi 400 ở mọi mức effort;
- body tới MaaS giống body Claude Code từng byte, trừ hai giá trị đó;
- cache hit 95,6% / 92,0%, bằng mức không có gateway sửa body (92,3% / 89,0%);
- xử lý body p95 7,3–7,8 ms với body 160K token.

Cách gateway đang làm (decode → sửa → encode lại bằng lua-cjson) cũng hết lỗi 400, nhưng cache hit chỉ còn
53–56%. Cấu hình, hướng dẫn chuyển, rollback và checklist: `deliverables/hubfix/`.

Lab: Huawei Cloud ECS Singapore, APISIX 3.11.0 (3.14.1 để kiểm chứng Admin API), MaaS `glm-5.3` endpoint
`ap-southeast-1`, Claude Code 2.1.296 (headless trên ECS) và 2.1.292 (Windows, đăng nhập Claude Pro),
ngày 2026-10-13. Chi tiết từng phase: `docs/hubfix-phase0.md`, `docs/hubfix-phase3.md`, các mục "Lab hub-fix"
trong `ASSUMPTIONS.md`.

## 1. Lỗi 400 và mapping đã chọn

Lab gửi thẳng tới MaaS các body Claude Code thật, mỗi lần đổi một trường (23 variant):

| Trường | Giá trị | HTTP | Lỗi của MaaS |
|---|---|---:|---|
| `output_config.effort` (top-level) | `low`, `high`, `max`, không có | 200 | |
| | `medium`, `xhigh` | **400** | `ModelArts.81001` `reasoning_effort must be [low, high, max] for glm-5.3` |
| `model` | `claude-sonnet-5-5` | **404** | `ModelArts.81009` `Invalid model.` (kiểm trước effort) |
| `messages[1].output_config.effort` | `medium` | 200 | MaaS không kiểm trường này |
| `thinking` | `adaptive`, `enabled` + `budget_tokens` | 200 | |
| `max_tokens` | 32000 / 64000 / 128000 | 200 | |

Claude Code chỉ đổi `output_config.effort` theo mức đã chọn; `thinking` luôn là `adaptive`. Mức mặc định của
Sonnet là `medium`, nên dev chọn Sonnet mà không chỉnh gì sẽ gặp 400 ngay từ request đầu tiên.

**Mapping đã duyệt:** `model` `claude-sonnet-*` → `glm-5.3`; top-level `output_config.effort` `medium → high`,
`xhigh → max` (làm tròn lên để không giảm chất lượng coding). Các giá trị khác, `thinking`, `max_tokens`,
`messages[1].output_config` giữ nguyên. Tên Claude khác (`claude-haiku-*`, `claude-opus-*`) không đổi.

## 2. So sánh các phương án

| Tiêu chí | P0 không đụng body (đối chứng) | P3 encode lại bằng cjson (giống gateway hiện tại) | **P4 sửa đúng byte** | P5 encode lại, key sắp xếp |
|---|---|---|---|---|
| 1. Lỗi 400 / non-200 | 0 | 0 | **0** | 0 |
| 2. Body giống client ngoài 2 trường (replay, 360 lần / cấu hình) | 360/360 | 0/360 | **360/360** | 0/360 |
| Số dạng byte / 30 lần gửi (1 / 4 worker) | 1 / 1 | 1–3 / 2–7 | **1 / 1** | 1 / 1 |
| 3. Cache hit, effort mặc định / xhigh (2 session mỗi ô) | 92,3% / 89,0% | 56,1% / 53,0% | **95,6% / 92,0%** | 85,5% / 88,4% |
| 4. Xử lý body p95, body 160K token (4 / 1 worker) | – | 9,4 / 8,5 ms | **7,3 / 7,8 ms** | 64 / 63 ms |

P0 dùng client tự gửi tên GLM, nên không phải đầu vào giống hệt; chênh lệch vài điểm giữa P0, P4 và P5 nằm trong
mức dao động giữa các lần chạy (3,9 và 10,7 điểm ở P0). Khoảng cách 35–40 điểm giữa P3 và P4 thì không phải nhiễu.
P6 (thư viện giữ thứ tự key gốc) không thử: không có trong APISIX 3.11.0 và 3.14.1.

## 3. Vì sao chọn P4

- Hết lỗi 400 như cách hiện tại, nhưng không tạo drift: mọi request của một session tới MaaS với cùng byte prefix,
  nên cache hit về mức P0.
- Chi phí thấp hơn cách hiện tại: một lần decode để xác nhận, so với decode + encode cả body.
- An toàn khi gặp body lạ: không định vị hoặc không xác nhận được vị trí thì chuyển tiếp body gốc và ghi log,
  không bao giờ encode lại cả body. Unit test 69/69 trên body thật và các trường hợp biên (khoảng trắng, CRLF, key
  trùng tên ở chỗ khác, key/giá trị viết bằng escape, thiếu trường, body 650–820 KB, JSON hỏng).
- P5 hết drift nhưng chậm khoảng 60 ms với body lớn, và cache hit vẫn thấp hơn P4.

## 4. Findings

**Đã xác nhận (có số liệu trong lab)**
- `glm-5.3` chỉ nhận effort `low`/`high`/`max`; `medium`/`xhigh` → 400 `ModelArts.81001`. Tên `claude-sonnet-5-5`
  → 404 `ModelArts.81009`. `thinking`, `max_tokens`, effort per-turn không gây lỗi.
- Encode lại cả body bằng lua-cjson đổi thứ tự key giữa các request (tới 7 dạng byte trên 30 lần gửi cùng một
  body), làm drift loại 1 ở 27–35% lượt chuyển và cache hit 50–58% trong session thật (Phase 3).
- Dấu vân tay của cách đó: `9007199254740991` thành `9.007199254741e+15` (giá trị đổi thành 9007199254741000).
- P4 giữ body từng byte (360/360), 0 lỗi, cache hit 89–96% trong 4 session, xử lý body p95 7,3–7,8 ms ở 160K token.
- `core.json.stably_encode` của APISIX dùng trực tiếp trên bảng do `core.json.decode` tạo ra: trả 500 khi body có
  `null` (Claude Code gửi `null` khi chạy ngoài git repo) và ghi `{}` thành `[]` trong schema của tool. Khuyến nghị
  "đổi một dòng sang `stably_encode`" trong báo cáo drift thứ tự key trước đây đã được đính chính.
- File bàn giao chạy được nguyên văn: `route.apisix.yaml` trên APISIX 3.11.0 standalone (config tối thiểu),
  `route.admin.json` qua Admin API của 3.14.1, 70/70 request đúng byte.

**Suy luận (chưa chứng minh trực tiếp)**
- Gateway production làm như P3 (biến thể A) hoặc dùng `ai-proxy` (biến thể B): cả hai đều encode lại cả body, nên
  đều gây drift; khách hàng xác nhận có sửa body nhưng lab chưa thấy cấu hình thật.
- P5 thấp hơn P4 vì sắp xếp key làm body khác với body các phiên khác đã cache; chưa tách riêng để đo.
- MaaS dịch `output_config.effort` sang tham số nội bộ `reasoning_effort` (theo câu thông báo lỗi).

**Giả định của lab**
- Tên model dev gửi là `claude-sonnet-5-5` (mặc định của `--model sonnet` trên Claude Code 2.1.29x).
- Gateway production chạy APISIX 3.x có `serverless-pre-function`.
- Kịch bản 6 prompt cố định (có MCP và subagent) đại diện đủ cho cách dev dùng; session thật chỉ chạy với 4 worker,
  mỗi ô 2 session.
- Giá dùng để ước chi phí là cận trên (cache read tính bằng giá input); giá cache của MaaS chưa biết.

**Cần xác minh với khách hàng hoặc MaaS**
- Cấu hình gateway thật (biến thể A hay B), danh sách plugin trên route, và tên model dev thực sự gửi; có cần map
  cả `claude-haiku-*` / `claude-opus-*` không (module hiện chỉ map một tiền tố).
- Phiên bản APISIX và giới hạn kích thước body của gateway.
- MaaS có dùng effort per-turn (`messages[].output_config`) không, và mức effort mặc định khi không gửi.
- MaaS có thể nhận đủ 5 mức effort và tự map (đề xuất trong `deliverables/hubfix/MAAS-SUMMARY.en.md`), để gateway
  không phải sửa body nữa.
- Cache MaaS giữ riêng trên từng instance, không có session affinity: nguồn giảm cache thứ hai, ngoài phạm vi lần
  sửa này.

## 5. Bàn giao (`deliverables/hubfix/`)

| File | Nội dung |
|---|---|
| `pre-function.lua` | Code `serverless-pre-function` của P4 (module + handler, có comment) |
| `route.apisix.yaml` / `route.admin.json` | Route ví dụ cho APISIX standalone và Admin API |
| `hubfix_patch.lua` | Riêng module, để `require` |
| `HUONG-DAN.md` | Điều kiện, chuyển từ biến thể A / B, triển khai an toàn, rollback, checklist production |
| `MAAS-SUMMARY.en.md` | Tóm tắt tiếng Anh gửi team MaaS, kèm bảng lỗi |

Các file Lua và route được sinh từ `apisix/hubfix/patch.lua` (`scenarios/hubfix_deliverables.py`) và kiểm bằng
`scenarios/hubfix_deliverable_test.sh`; unit test: `make test-hubfix`.
