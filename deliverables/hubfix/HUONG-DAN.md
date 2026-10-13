# Áp dụng P4 lên gateway: map Sonnet → GLM mà không encode lại body

**Thay đổi:** gateway chỉ thay đúng byte của hai giá trị trong body request của Claude Code, rồi chuyển tiếp phần
còn lại nguyên trạng. Không decode rồi encode lại cả body.

| Trường (top-level) | Giá trị vào | Giá trị ra |
|---|---|---|
| `model` | `claude-sonnet-*` | `glm-5.3` (biến `GLM_MODEL`) |
| `output_config.effort` | `medium` / `xhigh` | `high` / `max` |
| | `low`, `high`, `max`, không có, giá trị khác | giữ nguyên |

Request mang tên Claude khác (`claude-haiku-*`, `claude-opus-*`) được giữ nguyên. Nếu không xác định hoặc không
xác nhận được vị trí cần sửa, body gốc được chuyển tiếp nguyên trạng và có một dòng log
`hubfix: forwarding the original body: <lý do>`. Gateway không bao giờ encode lại cả body.

Đã kiểm chứng trong lab: 0 lỗi 400 ở mọi mức effort; body tới MaaS giống body Claude Code từng byte, ngoài hai
giá trị trên; cache hit 92–96%, bằng mức không có gateway sửa body (cách encode lại bằng lua-cjson: 53–56%); xử lý
body p95 7,3–7,8 ms với body 160K token. Chi tiết: báo cáo `hub-fix-report.md`.

## Các file

| File | Dùng khi |
|---|---|
| `pre-function.lua` | Toàn bộ code cho `serverless-pre-function` (`phase: rewrite`): module + handler. Dán nguyên vào `functions`. |
| `route.apisix.yaml` | APISIX standalone (`config_provider: yaml`): ví dụ route + upstream MaaS. |
| `route.admin.json` | Cùng route đó cho Admin API: `PUT /apisix/admin/routes/<id>`. |
| `hubfix_patch.lua` | Riêng module (`hubfix.patch`), nếu muốn đặt trong `extra_lua_path` và `require` thay vì dán vào function. |

Thay các chỗ giữ chỗ trước khi dùng: `<MAAS_HOST>` (ví dụ `api-ap-southeast-1.modelarts-maas.com`),
`<MAAS_PATH_PREFIX>` (ví dụ `/anthropic`), `<MAAS_AUTH_HEADER_VALUE>` (ví dụ `Bearer <key MaaS>`), và
`GLM_MODEL` ở đầu handler trong `pre-function.lua` (cũng nằm trong hai file route).

**Đã chạy thử nguyên văn các file này:** `route.apisix.yaml` trên APISIX 3.11.0 standalone với `config.yaml`
tối thiểu (buffer body mặc định), `route.admin.json` qua Admin API của APISIX 3.14.1. Ở cả hai, 70/70 request
(6 body Claude Code thật và body 160K token) tới upstream đúng từng byte như mong đợi.

## Điều kiện

- APISIX 3.x có plugin `serverless-pre-function` trong danh sách plugin (có sẵn mặc định). Code chỉ dùng
  `core.request.get_body`, `core.json.decode`, `ngx.req.set_body_data`, `core.log.warn`.
- Route không giới hạn kích thước body dưới khoảng 1 MB. Body 160K token khoảng 820 KB.
- **Không có plugin nào khác trên route decode rồi encode lại body.** Nếu còn, P4 mất tác dụng: ví dụ `ai-proxy`,
  `ai-proxy-multi`, `ai-prompt-decorator`, `ai-prompt-template`, `body-transformer`, hoặc một
  `serverless-*-function` khác có `core.json.encode`. Cách kiểm tra: mục "Checklist", bước 1.

## Chuyển từ cấu hình hiện tại

**Biến thể A: Hub có một đoạn Lua decode → sửa → encode** (`serverless-pre-function` hoặc plugin riêng).

1. Lưu lại cấu hình route hiện tại để rollback (xem mục "Rollback").
2. Trong đoạn Lua đó, xoá phần decode / sửa `model` và thinking / `core.json.encode` / `set_body_data`. Giữ các phần
   khác nếu có, ví dụ đọc header hay ghi log.
3. Thêm `pre-function.lua` vào `functions` của `serverless-pre-function` trên route, ở `phase: rewrite`. Nếu route
   đã có `serverless-pre-function`, thêm nó thành một phần tử nữa trong mảng `functions`, đặt **trước** đoạn nào
   cần đọc `model` đã map.
4. Nếu Hub còn map thêm tên model khác (ví dụ `claude-haiku-*` → GLM), báo lại để mở rộng module: hiện module chỉ
   map một tiền tố (`sonnet_prefix`).

**Biến thể B: `ai-proxy` / `ai-proxy-multi` + Lua cho thinking.** `ai-proxy` tự decode và encode lại body (lab
đo được: thứ tự key đổi ở mọi request, thêm `stream_options`), nên chỉ thêm P4 thì chưa đủ.

1. Lưu lại cấu hình route hiện tại.
2. Bỏ `ai-proxy` / `ai-proxy-multi` và đoạn Lua thinking khỏi route.
3. Đặt upstream trỏ thẳng tới MaaS và `proxy-rewrite` cho đường dẫn và header xác thực, như `route.apisix.yaml` /
   `route.admin.json`. Thêm `pre-function.lua`.
4. Những gì `ai-proxy` đang làm phải được thay bằng cách khác nếu Hub cần: chọn model (P4 đã làm), failover giữa
   nhiều provider (`ai-proxy-multi`), thống kê token. Upstream nhiều node và `retries` của APISIX thay được phần
   failover cơ bản.

## Triển khai an toàn

1. **Route thử trước.** Tạo một route giống route production (cùng plugin), khác URI hoặc host, upstream là echo
   server của `replay.py`. Chạy checklist bước 1–2 trên route thử.
2. Áp dụng lên route production lúc ít tải. Phiên Claude Code đầu tiên sau khi đổi sẽ không dùng lại cache tạo
   trước đó (byte đầu vào khác); việc này chỉ xảy ra một lần.
3. Theo dõi checklist bước 3–5 trong 1–2 ngày.

## Rollback

- **Standalone (`conf/apisix.yaml`):** giữ bản sao file trước khi sửa. Rollback = chép lại bản cũ. APISIX tự nạp
  lại khi file đổi (file phải kết thúc bằng dòng `#END`).
- **Admin API:** trước khi sửa, `GET /apisix/admin/routes/<id>` và lưu `value` ra file. Rollback =
  `PUT /apisix/admin/routes/<id>` với đúng nội dung đó.
- Rollback chỉ đưa cách sửa body về như cũ. Lỗi 400 và mức cache hit thấp cũng sẽ quay lại như trước.

## Checklist kiểm chứng trên production

1. **Replay trên route thử** (`replay.py`, hướng dẫn trong `scenarios/replay/README.md` của repo lab): gửi một
   body Claude Code thật 30 lần, concurrency 1 và 8. Đạt khi cột `distinct bytes` = **1** và `cjson number` =
   **0**. Cách encode lại bằng cjson trong lab cho tới 7 dạng byte trên 30 lần (4 worker) và `cjson number` = 30.
2. **Không còn dấu vân tay cjson:** body upstream nhận không chứa chuỗi `9.007199254741e+15`. Claude Code gửi số
   `9007199254740991` trong schema của tool; lua-cjson in lại thành chuỗi trên.
3. **Lỗi 400 = 0:** trong access log của gateway, đếm `upstream_status` 400 (MaaS: `ModelArts.81001`
   `reasoning_effort must be [low, high, max]`) và 404 (`ModelArts.81009` `Invalid model.`) trước và sau khi đổi.
4. **Log fallback:** số dòng `hubfix: forwarding the original body` trong error log. Bình thường bằng 0; nếu có,
   lý do nằm ngay trong dòng log.
5. **Cache hit trước / sau:** từ `usage` trong response của MaaS (với stream, số nằm ở event `message_delta`):
   `cache_read_input_tokens / (input_tokens + cache_read_input_tokens + cache_creation_input_tokens)`, gộp theo
   ngày, so ít nhất 1 ngày trước và 1 ngày sau. Lab: cách encode lại bằng cjson 53–56%, P4 92–96%.
6. **Overhead:** p95 của `request_time - upstream_response_time` trong access log, trước và sau. Lab: phần xử lý
   body của P4 thêm khoảng 7–8 ms p95 với body 160K token, thấp hơn cách encode lại bằng cjson (8,5–9,4 ms).
