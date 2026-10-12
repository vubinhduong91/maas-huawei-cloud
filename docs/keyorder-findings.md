## Findings: drift thứ tự key làm giảm cache hit

Câu hỏi: trên đường `Claude Code → gateway APISIX → Huawei MaaS (GLM)`, request cùng một session có lúc mang
thứ tự key khác trong `tools[].input_schema.properties` (và các dict khác), làm cache hit thấp hơn hẳn so
với cùng client gọi model khác. Lớp nào gây ra, ảnh hưởng tới cache bao nhiêu, và sửa thế nào?

Dữ liệu: lab trên ECS Singapore (Docker CE, APISIX 3.11.0, thêm 3.14.1 cho ai-proxy), Claude Code 2.1.296
headless, MaaS `glm-5.3` endpoint `ap-southeast-1`. Đường đo: `Claude Code → tap-pre → APISIX → tap-post →
MaaS`; hai tap byte-transparent, ghi nguyên bytes để so sánh offline (`tap/keyorder.py`). Chi tiết từng phase
và cấu hình: `ASSUMPTIONS.md` (các mục "Lab key-order drift"); bảng gốc ở phần phụ lục của báo cáo này.

### 1. Kết luận

**Drift sinh ra ở gateway, khi một plugin decode body rồi encode lại bằng lua-cjson** (`core.json.encode`).
Lua table không giữ thứ tự key, nên cùng một body vào có thể ra nhiều thứ tự key khác nhau. MaaS render
`tools` ở đầu prompt, nên đổi thứ tự key ở tool k thì cache chỉ còn phần trước tool k. Encode bằng
`core.json.stably_encode` (hoặc sắp xếp key trước MaaS) làm hết drift và đưa cache hit về mức không có gateway
đụng body.

### 2. Sự thật đã xác nhận (có số liệu)

1. **Claude Code và MCP server không gây drift.** 3 session thật (built-in + MCP `lab-tfs`, MCP server khởi
   động lại trước mỗi prompt), 215 request, 209 lần so sánh N với N−1: **0** lần đổi thứ tự key ở cả 4 tầng
   (top-level, content block, `tools[].input_schema.properties`, `tool_use.input`). Log tap cũ của lab cache
   (31 phiên, 360 lần so sánh): 0. (Phase 1–2)
2. **Gateway không đụng body thì không có drift.** P0 (key-auth + đổi header): body tại tap-pre và tap-post
   trùng byte 11/11 (Phase 1), 0/41 body khác ở Phase 4; replay 30 lần × 3 body × concurrency 1/8, sau
   `nginx -s reload`, sau restart: **đúng 1 dạng byte** mỗi body (360/360 còn nguyên). (Phase 1, 4, 5)
3. **Plugin decode + encode lại bằng cjson tái hiện đúng signature của production.** P2 (`serverless-pre-function`
   decode → `core.json.encode` → `set_body_data`) và P1 (`ai-proxy`, cũng encode bằng cjson):
   - thứ tự mới **không theo alphabet**, ở **cả 4 tầng dict**, cả tool built-in lẫn MCP;
   - **mảng giữ nguyên thứ tự**, nội dung sau khi sort key giống hệt (chỉ thứ tự đổi);
   - thứ tự **đổi giữa các request cùng session**: P2 4 worker 55% lần so sánh có drift ở `tools`, 3 thứ tự
     khác nhau cho tool `Agent` trong một session;
   - replay cùng một body 30 lần: **2–9 thứ tự key khác nhau**, body càng lớn càng nhiều; 1 worker vẫn drift
     (tới 6 thứ tự / 30 lần); reload hay restart không hết. (Phase 4, 5)
4. **Tác động cache phụ thuộc vị trí tool bị đổi** (body 56,8K token, gọi thẳng MaaS): đổi ở tool 0 → còn
   **448 token (0,8%)**; tool giữa ~8,4K (15%); `TaskUpdate` 12.480 (22%); tool cuối 14.976 (26%). Đổi thứ tự
   key top-level hoặc trong content block của `messages` → vẫn hit 99,9% (MaaS chuẩn hoá phần này). Chỉ thứ tự
   trong `tools` làm mất cache. (Phase 3)
5. **Cache hit thực tế của session Claude Code giảm tương ứng:** P0 86,8–93,8%; P2 4 worker 61,8–62,2%.
   (Phase 4, 6)
6. **Hai cách sửa đã kiểm chứng** (session Claude Code thật, P2 4 worker, Phase 6):

   | Cấu hình | Drift ra khỏi gateway | Drift tới MaaS | Cache hit |
   |---|---:|---:|---:|
   | P2, không sửa | 43% | 43% | 62,2% |
   | P2 + sắp xếp key trước MaaS (canon) | 45% | **0%** | 82,8% |
   | P2S: encode bằng `core.json.stably_encode` | **0%** | **0%** | **86,8%** |
   | Đối chứng: đảo key tool đầu / tool cuối | – | 100% | 2,2% / 56,6% |

   Replay P2S: 1 thứ tự key trên 360 lần gửi (P2 cùng run: 2–9). Không fix nào làm hỏng session (6/6 prompt,
   0 lỗi HTTP).
7. **Dấu vân tay cjson trên đường truyền:** `9007199254740991` (`Number.MAX_SAFE_INTEGER` trong schema của
   `Read.offset/limit` và vài tool khác) thành **`9.007199254741e+15`** sau khi đi qua cjson (2.160/2.160 body
   qua P1/P2; 720/720 body qua P0 giữ nguyên). P1 còn thêm `stream_options` vào body. Dấu vân tay vẫn có ở P2S
   (cjson làm tròn lúc decode) nhưng ổn định giữa các lần gửi nên không ảnh hưởng cache.

### 3. Suy luận (chưa chứng minh trực tiếp trên production)

- Tỉ lệ drift production (7,31% request không phải đầu session) nằm giữa mức đo được của P2 1 worker và
  4 worker. Nó phụ thuộc số worker, trạng thái bảng hash Lua trong từng worker và kích thước body, nên không
  kỳ vọng lab ra đúng con số này.
- Production báo cache còn 36,5% khi tool bị đổi là `TaskUpdate` ở cuối danh sách; lab đo 22–26% cho vị trí
  tương tự. Cùng cơ chế (cắt prefix tại tool), tỉ lệ khác vì tỉ trọng tools / hội thoại khác.
- Lớp gây lỗi trên production chưa được nhìn trực tiếp (không có source gateway). Bằng chứng gián tiếp: cùng
  Claude Code gọi thẳng MaaS không thấy drift, và signature (4 tầng, không alphabet, mảng giữ nguyên, đổi trong
  session) khớp từng điểm với cjson re-encode.

### 4. Khuyến nghị (theo thứ tự ưu tiên)

1. **Không decode + encode lại body** khi plugin không cần sửa body (đọc field thì decode bản copy, forward
   nguyên bytes). Đây là cấu hình có hit cao nhất (P0).
2. Nếu plugin bắt buộc sửa body: **thay `core.json.encode` bằng `core.json.stably_encode`** (có sẵn trong
   APISIX `core/json.lua`, sắp xếp key, cùng input cho cùng bytes).
3. Nếu không sửa được gateway: **sắp xếp key toàn body tại ingress MaaS** (hoặc một proxy ngay trước MaaS).
4. Khi đổi cách encode, cache cũ (tạo từ thứ tự key khác) không được dùng lại, chỉ mất một lần.

### 5. Kiểm chứng trên production mà không cần tái hiện

- **Dấu vân tay:** tìm `9.007199254741e+15` trong body MaaS nhận được từ production. Có thì body đã đi qua
  cjson decode + encode ở đâu đó trên đường.
- **Replay:** `scenarios/replay/replay.py` (một file, stdlib, chỉ ghi hash, không ghi nội dung). Team gateway
  gửi cùng một body Claude Code 30 lần qua gateway tới một echo server rồi xem cột `distinct key orders`:
  gateway không đụng body ra 1; gateway re-encode bằng cjson ra > 1. Hướng dẫn: `scenarios/replay/README.md`.

### 6. Giới hạn

- Chưa có danh sách plugin của gateway production, nên profile P3 ("đúng chuỗi plugin production") chưa làm;
  P1/P2 là mô phỏng hai kiểu plugin phổ biến có sửa body.
- Mỗi cấu hình Claude Code thật chỉ có 1 session ngắn (Phase 4, 6); replay (1.620 request ở Phase 5 + 720 ở Phase 6) bù cho cỡ
  mẫu nhỏ ở phần drift, không bù cho phần cache hit.
- Chưa đo chi phí CPU / độ trễ của `stably_encode` (dkjson thuần Lua) trên body ~290 KB ở tải production.
- Cache hit trên MaaS có nhiễu routing (nhiều instance, không có session affinity): giá trị lệch ở Phase 3 đều
  giải thích được bằng prefix chung với request khác, nhưng hit theo từng request có độ lệch tự nhiên.
