## Findings

Dữ liệu: các session có nhãn kịch bản trên ECS Singapore (mode B: Claude Code → APISIX → tap → MaaS
`glm-5.3`, endpoint pay-per-use `ap-southeast-1`), Claude Code 2.1.296, repo mẫu Reaparr (.NET 10 +
Vue 3). Run `20261011-181222` (S1–S8) và `20261011-221627` (S9): **355 request (331 OK), 22 session,
15,2M token input, weighted hit 89,1%**. Số liệu bên dưới lấy từ `requests.csv` / các bảng phía trên
của báo cáo này; Phase 2–3 lấy từ `ASSUMPTIONS.md`.

### 1. Sự thật đã xác nhận (có số liệu)

1. **Gateway của lab không đổi request.** Body APISIX nhận và body tap nhận trùng sha256 ở 7/7 request,
   kể cả body 623.529 byte (~164K token); 0 khác biệt ở `anthropic-version`, `anthropic-beta`,
   `user-agent`, `x-claude-code-session-id` (Phase 2). S1: qua APISIX và gọi thẳng đều đạt 99,9% sau lần
   gửi đầu.
2. **Claude Code giữ prefix ổn định.** 306/333 lượt chuyển trong cùng session là append-only; lệch chỉ do
   compaction (12, đều ở S6), system-reminder (7), khác (8). **0** lần lệch do ngày giờ, git status, cwd
   hay thứ tự tool. Marker `cache_control` dời chỗ ở 288/333 lượt nhưng không làm đổi text prefix.
3. **MaaS tự cache theo prefix, không cần marker.** S8: bỏ toàn bộ `cache_control` vẫn hit 99,9% ở lần
   gửi 2, 3, 4.
4. **Cách MaaS báo usage:** `cache_creation_input_tokens` = 0 ở **tất cả** 318 request GLM (phần ghi cache
   nằm trong `input_tokens`); `cache_read_input_tokens` luôn là bội số của 64. MaaS đặt `tools` trước
   `system` (nonce ở `system[0]` vẫn để 53,5% prefix — phần tools — được dùng lại).
5. **Request y hệt gửi lại sau vài giây có lúc miss.** S1 qua APISIX: 0 → 99,9 → 99,9 → 99,9 → **0**;
   gọi thẳng: 0 → 99,9 → 99,9 → **0** → 99,9; S8 có marker: 0 → 99,9 → **0** → 99,9.
6. **Miss phía serving:** 19/307 request đủ điều kiện xét (6%) có prefix đã gửi trước đó nhưng MaaS không
   dùng lại; **14/19** xảy ra khi khoảng nghỉ so với request trước chỉ 0–1 s (không phải do TTL). Trên toàn
   bộ request được xét, MaaS đọc từ cache **95,5%** lượng token có thể dùng lại.
7. **Hit cao là đạt được trên MaaS:** S2 (15 turn coding, tới 125.900 token/request) **96,8%**; S5 (3 dev
   song song) 91,9%; S3 (phiên mới sau 60 s dùng lại system + tools + CLAUDE.md) 99,3%; S9 cùng 5 turn:
   **glm-5.3 95,4%** vs **Claude Sonnet 89,5%** (Claude: request đầu lạnh 46K token cache_creation).
8. **Compaction làm tụt cache:** S6 weighted 61,0%, quanh lượt compact còn ~50%.
9. **Giới hạn tốc độ thấp:** S5 (chỉ 3 phiên Claude Code song song) nhận **24 lỗi 429** trong 33 giây,
   `ModelArts.81114 "Too many requests, the rate limit is 1000000 tokens per minute"`.
10. **TTFT:** glm-5.3 P50 2,37 s / P95 3,51 s; Claude Sonnet (S9) P50 1,06 s.
11. MaaS nhận cả `x-api-key` và `Authorization: Bearer`; `/v1/messages/count_tokens` → 404; chấp nhận bộ
    beta của Claude Code (kể cả `context-1m-2025-08-07`, `extended-cache-ttl`) và `cache_control` có
    `ttl: "1h"`. Tên model Claude gửi tới MaaS → 404 `ModelArts.81009`.

### 2. Suy luận (inference, chưa chứng minh trực tiếp)

1. Khoảng cách 73–75% (GLM) vs ~92% (Sonnet) ở production **không đến từ Claude Code**, và không đến từ
   một gateway chuyển tiếp nguyên trạng như APISIX của lab (mục 1.1, 1.2, 1.7).
2. MaaS có **nhiều replica, cache prefix riêng từng replica, routing không theo session/prefix**: hit/miss
   xen kẽ khi gửi lại y hệt trong vài giây (1.5) và miss ở khoảng nghỉ 0–1 s (1.6) khớp với mô hình này
   hơn là TTL hay eviction. Ở tải production (nhiều dev, nhiều replica hơn) xác suất trúng đúng replica
   thấp hơn → đây là cơ chế hợp lý nhất cho mức 73–75%.
3. Claude Code coi model lạ là context 200K và **tự compact sớm**. Với input trung bình ~160K
   token/request ở production, phiên sẽ chạm ngưỡng 200K thường xuyên → compaction thường xuyên → hit tụt
   như S6 (1.8). Trong lab đã khai báo context thật 1M nên không gặp.
4. Giới hạn 1M token/phút nhiều khả năng **tính cả token đọc từ cache** (3 phiên × ~45K token/request).
   Nếu đúng, với ~160K token/request cả tài khoản chỉ được ~6 request/phút; 429 + retry kéo dài khoảng
   nghỉ và làm phiên bị dồn cục.
5. S4 không đo được TTL: probe sau 30/60/120 s **miss**, sau 180/300 s **hit**, sau 600/900 s miss — nhiễu
   do routing lấn át TTL. Có thể nói TTL ≥ 5 phút trên ít nhất một replica.

### 3. Giả định

1. Cấu hình APISIX thật ở production không có trong tay; lab chỉ chứng minh cho cấu hình của lab.
2. Endpoint pay-per-use `ap-southeast-1`; production có thể dùng Token Plan (cụm khác, cache có thể khác).
3. Repo mẫu khác nghiệp vụ production; ECS lab ở Singapore gọi MaaS Hong Kong; tối đa 3 dev song song.
4. Giá Huawei chưa có: chi phí trong báo cáo là **cận trên** (giá Z.ai, cache read tính bằng giá input):
   ≤ $21,21 cho 318 request GLM.
5. So sánh với Claude dùng subscription qua Claude Code, mẫu nhỏ (13 request) — đủ để so thứ tự độ lớn,
   không đủ để so chính xác.
6. Token của prefix trong báo cáo là ước lượng (không có tokenizer GLM).

### 4. Cần xác minh với MaaS backend team

1. `glm-5.3` cho tài khoản/region này chạy trên bao nhiêu replica? Routing có theo session hoặc theo
   prefix (prefix-aware / KV-cache-aware) không? Có bật được session affinity (ví dụ theo
   `X-Claude-Code-Session-Id` hoặc `metadata.user_id.session_id`) không?
2. Prefix cache là riêng từng replica hay dùng chung? TTL và chính sách eviction là gì?
3. Vì sao `cache_creation_input_tokens` luôn = 0? Token đọc từ cache được tính giá thế nào?
4. Giới hạn 1M token/phút có tính token đọc từ cache không? Nâng được không, theo tài khoản hay theo key?
5. Xác nhận block cache 64 token; MaaS có dùng `cache_control` / `ttl` không?
6. Endpoint Token Plan (`api.modelarts-maas.com/plan/anthropic`) có cùng hành vi cache như pay-per-use?

### Khuyến nghị (theo thứ tự ưu tiên)

1. **Phía MaaS: routing giữ session/prefix (affinity) hoặc cache dùng chung giữa replica.** Đây là nguồn
   mất cache duy nhất còn lại trong lab (mục 1.5, 1.6) và nhiều khả năng lớn hơn ở production. Không đòi
   hỏi thay đổi gì ở client.
2. **Khai báo context thật của GLM-5.3 (1M) cho Claude Code của dev**, để Claude Code không tự compact ở
   200K: `ANTHROPIC_MODEL=glm-5.3[1m]` (hoặc `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000` khi mọi tier đều là
   GLM). Tăng context dùng được, không giảm; tránh cú tụt hit sau compaction (S6: 61%).
3. **Làm rõ và nâng giới hạn 1M token/phút** (mục 1.9). Nếu cache read bị tính vào giới hạn, tải coding
   thật sẽ thường xuyên bị 429.
4. **Gateway production giữ nguyên request và giữ một session trên cùng một đường đi**: không ghi lại
   body, không chèn nội dung vào prompt; nếu gateway cân bằng tải qua nhiều endpoint/key/tài khoản MaaS
   thì băm theo `X-Claude-Code-Session-Id` để một phiên không bị chia giữa các cụm cache.
5. **Đo production bằng cùng phương pháp** trước khi thay đổi lớn: đặt tap (hoặc log body + usage) sau
   gateway production cho vài dev trong một ngày rồi chạy `analyze.py`, để biết production mất cache do
   lệch prefix (phía client) hay do miss phía serving.
