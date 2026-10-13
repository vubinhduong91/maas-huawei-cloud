## Hub-fix Phase 3: đo và so sánh P0 / P3 / P4 / P5

**P4 (sửa đúng byte) đạt cả 4 tiêu chí của spec. Cache hit của nó bằng hoặc cao hơn P0 (95,6% / 92,0% so với
92,3% / 89,0%).** P3, tức cách Hub đang làm, hết lỗi 400 nhưng chỉ còn 53–56% cache hit. P5 (encode lại với
thứ tự key ổn định) hết drift nhưng thấp hơn P4 3–10 điểm và chậm gấp khoảng 10 lần. Overhead p95 của P4 ở body
160K token là 9,8 ms (4 worker) và 11,1 ms (1 worker): hơi vượt ngưỡng 10 ms ở 1 worker, nhưng chỉ chậm hơn cách
Hub đang làm (P3) khoảng 1–2 ms.

Môi trường: ECS Singapore, APISIX 3.11.0, MaaS `glm-5.3` (`ap-southeast-1`), Claude Code 2.1.292 trên Windows
(đăng nhập Claude Pro), ngày 2026-10-13. Mapping đã duyệt ở Phase 0 (`docs/hubfix-phase0.md`): Sonnet →
`glm-5.3`, effort `medium → high`, `xhigh → max`. Cách chạy và số liệu gốc: `ASSUMPTIONS.md`, các mục "Lab
hub-fix"; report trên ECS: `reports/hubfix-phase3-*.md`.

| Cấu hình | Cách sửa body |
|---|---|
| P0 | Không đụng body. Client tự gửi tên GLM và mức effort GLM nhận (đối chứng) |
| P3 | Decode, sửa, `core.json.encode` (lua-cjson) cả body: mô phỏng Hub hiện tại |
| **P4** | Chỉ thay byte của `model` và `output_config.effort` ở top-level; không xác nhận được thì chuyển tiếp nguyên trạng (`apisix/hubfix/patch.lua`) |
| P5 | Decode, sửa, encode cả body với key sắp xếp, đã xử lý `null` và `{}` (`apisix/hubfix/stable_encode.lua`) |

### 1. So sánh theo 4 tiêu chí

| Tiêu chí (spec mục 4) | P0 | P3 | **P4** | P5 |
|---|---|---|---|---|
| Lỗi 400 / non-200 (16 session + 7 case mỗi profile) | 0 | 0 | **0** | 0 |
| Body tới MaaS giống client ngoài 2 trường map (replay, 360 lần / cấu hình) | 360/360 | 0/360 | **360/360** | 0/360 |
| Số dạng byte trên 30 lần gửi cùng body (1 / 4 worker) | 1 / 1 | 1–3 / 2–7 | **1 / 1** | 1 / 1 |
| Dấu vân tay `9.007199254741e+15` | 0 | 360/360 | **0** | 360/360 |
| Cache hit MaaS, effort mặc định (2 session, gộp token) | 92,3% | 56,1% | **95,6%** | 85,5% |
| Cache hit MaaS, effort xhigh / max (2 session) | 89,0% | 53,0% | **92,0%** | 88,4% |
| Drift loại 1 ra khỏi gateway (session) | 0% | 28–32% | **0%** | 0% |
| Xử lý body, p95, body 160K token (4 / 1 worker) | – | 9,2 / 8,8 ms | **9,8 / 11,1 ms** | 64 / 63 ms |

Ở replay, P0 nhận body đã map sẵn, nên "giống client" ở cột P0 nghĩa là giống body mà client đó gửi.

### 2. Cache hit trong session thật

Mỗi session gồm 6 prompt cố định (`keyorder-short.txt`, có MCP và subagent), repo Reaparr cùng commit, gateway
4 worker. Thứ tự chạy xoay vòng P0 → P3 → P4 → P5, 2 lượt, vì MaaS giữ cache riêng trên từng instance và không có
session affinity.

| Effort | P0 (lượt 1 / 2) | P3 | **P4** | P5 |
|---|---|---|---|---|
| Mặc định (Sonnet `medium → high`; P0 `high`) | 90,2% / 94,1% | 54,4% / 58,0% | **94,9% / 96,3%** | 82,2% / 89,2% |
| xhigh (Sonnet `xhigh → max`; P0 `max`) | 84,0% / 94,7% | 55,4% / 50,5% | **89,0% / 95,1%** | 84,1% / 90,6% |

- **P4 không thấp hơn P0 ở lượt nào.** Spec yêu cầu không thấp hơn P0 quá mức dao động giữa các lần chạy P0.
  Mức dao động đó là 3,9 điểm (effort mặc định) và 10,7 điểm (xhigh).
- **Vì sao P4 nhỉnh hơn P0:** body hai bên khác nhau ở phía client. Sonnet gửi thêm message per-turn, `max_tokens`
  khác, còn P0 dùng `glm-5.3[1m]`. Chênh 2–3 điểm nằm trong mức dao động, không phải P4 "tốt hơn" không đụng body.
- **P5 thấp hơn P4** dù 0 drift. Sắp xếp key làm prefix của body khác với body mà phiên trước và các phiên khác
  đã cache, vì những body đó giữ thứ tự gốc của Claude Code. Đây là suy luận, chưa tách riêng để đo.
- **P3 thấp vì drift 28–32%.** Khớp với Phase 1 (54,9% / 62,1%) và lab key-order (P2: 62,2%).
- Có 1 session lệch: `p3-P5-xhigh-r2` có 39 request (các session khác 19–24), một prompt chạy 454 giây và
  2 tool lỗi ở request cuối, nhưng không có lỗi HTTP. Chỉ thấy một lần, nên không đủ để kết luận về P5.

### 3. Overhead

Đo CPU time của phần xử lý body trong pre-function (`os.clock`, sau khi đã đọc body), 100 request mỗi ô,
concurrency 1, upstream là echo. Body 50K = 230 KB = 49.616 token; body 160K = 820 KB = 159.220 token (đếm trên
MaaS).

| Cấu hình | 50K p50 / p95 (ms) | 160K p50 / p95 (ms) | Thêm vào end-to-end so với P0, 160K p50 (ms) |
|---|---|---|---:|
| P3, 4 worker | 1,7 / 5,8 | 4,6 / 9,2 | 4,7 |
| P3, 1 worker | 1,6 / 4,6 | 4,4 / 8,8 | 4,0 |
| **P4, 4 worker** | 2,1 / 6,0 | 5,4 / **9,8** | 4,4 |
| **P4, 1 worker** | 2,3 / 6,3 | 6,0 / **11,1** | 5,3 |
| P5, 4 worker | 12,4 / 15,9 | 59,7 / 64,1 | 58,4 |
| P5, 1 worker | 12,2 / 15,9 | 59,3 / 63,5 | 59,3 |

- **P4 vượt 10 ms ở 1 worker (p95 11,1 ms).** Lý do: P4 decode cả body hai lần, một lần để đọc giá trị và một lần
  để xác nhận kết quả, cộng thêm bước quét chuỗi. P3 decode một lần và encode một lần.
- **Cách giảm** (chưa làm, chờ duyệt): đọc `model`/`effort` thẳng từ bước quét thay vì decode lần đầu. Như vậy
  chỉ còn một lần decode để xác nhận, ước giảm khoảng 2 ms ở 160K. Bỏ cả bước decode xác nhận thì nhanh hơn nữa,
  nhưng mất lớp kiểm tra mà spec yêu cầu.
- So với Hub hiện tại (P3), P4 chỉ thêm khoảng 0,6–2,3 ms p95 ở 160K. P5 tốn 60–64 ms vì dkjson viết bằng Lua
  thuần; không nên dùng ở body lớn.

### 4. Khuyến nghị

**Dùng P4.** P4 hết lỗi 400 như P3, giữ body client từng byte (trừ hai giá trị) và đưa cache hit về mức P0, với
chi phí xử lý gần bằng cách Hub đang làm. P5 chỉ nên dùng khi Hub bắt buộc phải encode lại cả body vì lý do khác.
Không dùng thẳng `core.json.stably_encode` (lỗi 500 khi body có `null`, đổi `{}` thành `[]`).

### 5. Chi phí và giới hạn

- 16 session: 361 request MaaS, 2,0M token input + 9,0M cache read + 92K output, khoảng $15,8 theo giá cận trên
  của lab (cache read tính bằng giá input). Hiệu chỉnh body overhead thêm khoảng 0,3M token. Replay: 0 token.
- Session thật chỉ chạy với 4 worker (đã thống nhất); khác biệt giữa 1 và 4 worker được đo bằng replay.
- Mỗi ô có 2 session; cache MaaS có nhiễu routing, nên chênh lệch vài điểm giữa P0, P4 và P5 không có ý nghĩa
  thống kê. Khoảng cách 35–40 điểm giữa P3 và P4 thì có.
- Chưa thử P6 (thư viện giữ thứ tự key gốc): không có sẵn trong APISIX 3.11.0 và 3.14.1.
