## Hub-fix: test bổ sung P4 so với P0

**P4 đạt mọi case.** Trên 14.037 body chạy offline, 300 request live và 18 session Claude Code thật, body P4 gửi
tới MaaS khớp đúng byte với kỳ vọng (body client, chỉ thay `model` và `output_config.effort`). Không có request
nào lỗi do P4. Cache hit của P4 ngang P0, trong mức dao động giữa các lần chạy. Test tìm ra một lỗi của P4 và đã
sửa: tên model có ký tự ngoài `[A-Za-z0-9._-]` (ví dụ `claude-sonnet-5-5[1m]`) trước đây bị chuyển tiếp nguyên
trạng, nên MaaS trả 404 (commit `6666f04`).

Môi trường: ECS Singapore, APISIX 3.11.0, MaaS `glm-5.3`, Claude Code 2.1.296 trên Windows (AChan-Computa, đăng
nhập Claude Pro), ngày 2026-10-13. Mapping như Phase 0: Sonnet → `glm-5.3`, effort `medium → high`,
`xhigh → max`. Report gốc trên ECS: `reports/hubfix-corpus.md`, `reports/hubfix-p4p0/`.

### 1. Offline: unit test và corpus

| Test | Số case | Kết quả |
|---|---:|---|
| Unit test `make test-hubfix` (thêm 27 case biên) | 96 | 96/96 |
| Corpus: mọi body ở tap-pre, nguyên trạng | 8.037 | 0 lỗi |
| Corpus: 200 body Sonnet × 5 model × 6 effort | 6.000 | 0 lỗi |
| File bàn giao trên APISIX 3.11 và 3.14.1 (`make test-hubfix-deliverables`) | 140 | PASS |

- **Case biên mới:** `count_tokens`, `stream` false hoặc không có, không có `thinking`, `thinking` disabled hoặc
  có budget, `output_config` có thêm member, `null` ở `model`/`output_config`/`effort`, effort rỗng,
  `output_config` hoặc `effort` trùng key, rác sau JSON, hai object liền nhau, BOM, khoảng trắng đầu và cuối,
  các tên model `claude-sonnet-4-5-*`, `[1m]`, `@`, `claude-opus-*`, `claude-3-5-sonnet-*`, `sonnet`.
- **Oracle độc lập với code được test:** bộ quét độ sâu 1 dùng `json.JSONDecoder.raw_decode` của Python tìm vị trí
  hai giá trị, ghép ra byte kỳ vọng, và `json.loads(out)` phải bằng body gốc sau khi map (`scenarios/hubfix_corpus.py`).
- **Thời gian P4** trên corpus (LuaJIT): median 0,64 ms mỗi body, body ≥ 500 KB median 2,3 ms, max 4,6 ms.
- **Giới hạn đã biết:** body có BOM, hoặc nén gzip, không phải JSON nên P4 chuyển tiếp nguyên trạng (cjson
  cũng không decode được). Claude Code không gửi các dạng này.

### 2. Live: từng request qua gateway

Một body Sonnet thật (lượt đầu, 69 KB), đổi bằng byte giá trị `model`, `output_config.effort`, `stream`. Mỗi
request đối chiếu với bản ghi tap-post: đích đến, header `x-lab-hubfix`, sha256 body rời gateway, status
(`scenarios/hubfix_matrix.py`).

| Gateway | Request | Đạt |
|---|---:|---:|
| P0, worker auto | 62 | 62 |
| P0, 4 worker, burst 24 + body 820 KB | 87 | 87 |
| P4, 1 worker | 63 | 63 |
| P4, 4 worker, burst 24 (8 song song) + body 820 KB | 88 | 88 |

- **P4:** `claude-sonnet-5-5` và `claude-sonnet-4-5-*` ở cả 6 mức effort × stream true/false đều tới MaaS, trả
  200, body đúng byte oracle. `glm-5.3` effort medium/xhigh được sửa effort và trả 200, các mức khác giữ nguyên.
- **P0:** `glm-5.3` effort medium/xhigh bị MaaS trả 400. Đây là lỗi P4 khắc phục.
- **Haiku/opus:** ở cả hai cấu hình đều đi Anthropic với body nguyên trạng.
- **Body lỗi** (JSON cụt) qua P4: chuyển tiếp nguyên trạng, header `fallback: scan: unterminated string`, log
  APISIX có `hubfix: forwarding the original body`.
- **`/v1/messages/count_tokens`:** MaaS chưa publish endpoint này, trả 404 `APIG.0101` ở cả P0 và P4.
- **Burst 8 song song:** MaaS trả 429 cho 6/24 request ở P4 và 5/24 ở P0. Retry đều thành công. Đây là giới
  hạn tốc độ của MaaS, không liên quan P4.

### 3. Session Claude Code thật

18 session (kể cả 1 session thử và 1 session reload), gateway 4 worker, thứ tự xoay vòng P0 → P4, repo Reaparr cùng commit. P0 dùng client glm (tên GLM,
effort GLM nhận). P4 dùng client Sonnet (tên và effort của Claude Code). Kiểm byte từng request bằng
`scenarios/hubfix_session_bytes.py`, cache bằng `scenarios/hubfix_sessions.py`.

| Kiểm tra | P0 | P4 |
|---|---|---|
| Request tới MaaS đúng byte kỳ vọng | 172/172 | 223/223 (đều `patched`) |
| Request Anthropic (subagent haiku) đúng byte | 2/2 | 4/4 |
| HTTP khác 200 | 0 | 0 |
| Drift thứ tự key ra khỏi gateway | 0% | 0% |
| Prompt lỗi | 0 | 0 |

Cache hit MaaS, 12 session chính (6 prompt `keyorder-short.txt`, 3 lượt):

| Effort | P0 (lượt 1 / 2 / 3) | P4 (lượt 1 / 2 / 3) | Trung bình P0 / P4 |
|---|---|---|---|
| Mặc định (P0 `high`; Sonnet `medium → high`) | 94,7% / 96,2% / 97,2% | 94,0% / 95,1% / 96,7% | 96,0% / 95,3% |
| xhigh (P0 `max`; Sonnet `xhigh → max`) | 84,3% / 94,9% / 94,1% | 89,5% / 94,4% / 95,8% | 91,1% / 93,2% |

- **Effort mặc định:** P4 thấp hơn P0 ở cả 3 lượt, nhưng chỉ 0,5–1,1 điểm. Dao động giữa các lượt của chính P0 là
  2,5 điểm, và của P0 ở xhigh là 10,6 điểm. Body hai bên khác nhau ở phía client (Sonnet có message per-turn,
  `max_tokens` khác), nên chênh lệch nhỏ này không quy được cho P4.
- **Kịch bản thêm, 1 session mỗi bên:**

  | Kịch bản | P0 | P4 |
  |---|---|---|
  | Subagent model sonnet + haiku, `/compact` giữa session (8 prompt) | 92,8% | 95,0% |
  | Chạy ngoài git repo (body có `null`) | 94,3% | 92,9% |
  | Reload APISIX 2 lần giữa session | – | 95,1%, 21/21 đúng byte, 0 lỗi |

  Subagent sonnet đi MaaS, subagent haiku đi Anthropic. Sau `/compact`, request tiếp theo vẫn được sửa đúng.
  Request gửi ngay lúc reload (15:08:31, 15:09:17 UTC) vẫn trả 200.
- **`--resume`:** mỗi prompt sau prompt đầu đều chạy bằng `--resume` cùng session, nên trường hợp tiếp tục session
  cũ đã nằm sẵn trong mọi session.

### 4. Kết luận

P4 đúng trên mọi dạng request đã thử, không gây lỗi nào, giữ cache ngang P0 và vẫn đúng khi APISIX reload. Khuyến
nghị dùng P4 không đổi, với bản `patch.lua` sau commit `6666f04`.

Ghi chú khi đo: đồng hồ của AChan-Computa chậm hơn ECS khoảng 70 giây. `hubfix_sessions.py` giờ nới khung giờ
session ±180 giây (`--clock-skew`). Nếu không nới, phần lớn request bị loại khỏi phép tính cache.
