# Assumptions & cần xác minh

Ký hiệu: **[DOC]** có trong tài liệu Huawei, **[VERIFY]** cần kiểm chứng bằng lab hoặc hỏi MaaS/khách hàng, **[LAB]** quyết định thiết kế của lab.

## Môi trường
- [LAB] Lab chạy trên Linux VM/ECS (Docker Compose v2, Python 3.11+, bash, curl). Code được viết trên Windows; `.gitattributes` ép LF.
- Claude Code dùng trong lab: **2.1.296** (cài bằng native installer trong WSL, `~/.local/bin/claude`), trùng với bản trên máy viết code.

## Huawei MaaS (nguồn: [Claude Code guide](https://support.huaweicloud.com/intl/en-us/model-call-maas/model-call-060.html), [Anthropic-compatible API](https://support.huaweicloud.com/intl/en-us/model-call-maas/model-call-022.html), tra cứu ngày 2026-10-11)
- [DOC] Endpoint: `https://api-ap-southeast-1.modelarts-maas.com/anthropic/v1/messages`, chỉ hỗ trợ region CN-Hong Kong.
- [DOC] Có endpoint riêng cho Token Plan: `https://api.modelarts-maas.com/plan/anthropic` (Tây Nam-Quý Dương 1). **[VERIFY] khách hàng dùng endpoint nào** — cache có thể khác giữa hai cụm.
- [DOC] Model: `glm-5.3`, `glm-5.2`, `glm-5.1`, `deepseek-v4-pro`, `deepseek-v4-flash`, `deepseek-v4.1-flash`.
- [DOC] Auth: bảng API ghi `x-api-key`, nhưng ví dụ Python/curl và hướng dẫn Claude Code (`ANTHROPIC_AUTH_TOKEN`) dùng `Authorization: Bearer`. Lab mặc định **Bearer** vì đó là đúng đường đi của Claude Code. [VERIFY] bằng `make probe`.
- [DOC] `usage` có `cache_creation_input_tokens`, `cache_read_input_tokens`.
- [DOC] `thinking` mặc định `enabled` với GLM; `max_tokens` mặc định 4K.
- [VERIFY] Tài liệu không nhắc `cache_control`, `metadata`, `anthropic-beta`, `count_tokens`, cache TTL, rate limit. TTL sẽ đo ở S4, `cache_control` ở S8, `count_tokens` bằng `make probe`.
- [VERIFY] Không có header bắt buộc khác (project id, region) — tài liệu không nêu.

## APISIX (Phase 1)
- [LAB] `apache/apisix:3.11.0-debian`, standalone (`config_provider: yaml`), không etcd, không Admin API.
- [LAB] Key MaaS được đưa vào bằng bước render (`apisix/render.py`) vào một docker volume; container APISIX không nhận key qua env, repo không chứa key.
- [LAB] `key-auth` chỉ đọc một header. Để chấp nhận cả `x-api-key` (ANTHROPIC_API_KEY) lẫn `Authorization: Bearer` (ANTHROPIC_AUTH_TOKEN), một `serverless-pre-function` chép bearer token sang `x-api-key` trước khi `key-auth` chạy. Chỉ đụng header, không đụng body. **Đây là khác biệt tiềm năng so với gateway production.**
- [LAB] Credential của dev bị xoá trước khi lên upstream (`hide_credentials` + `proxy-rewrite remove/set`), sau đó gắn key MaaS.
- [LAB] Route gồm `/v1/messages` và `/v1/messages/count_tokens`, forward nguyên query string (Claude Code gửi `?beta=true`). `count_tokens` được forward giống production; nếu MaaS trả 404 thì lab ghi nhận đúng hành vi đó.
- [LAB] `proxy_buffering off` ở mức http để SSE không bị buffer; smoke test đo khoảng cách giữa các event để chứng minh.
- [LAB] `file-logger` dùng `log_format` tường minh, **không ghi request header** (vì sau `proxy-rewrite` header chứa key MaaS). Body ghi qua `$request_body` (xem mục Tap về `client_body_buffer_size`); đã kiểm chứng ở Phase 2 với body ~164K token.
- [LAB] TLS tới MaaS: không bật verify cert upstream (mặc định nginx). Chỉ dùng cho lab.
- [VERIFY] **Cấu hình APISIX thật của gateway production không có trong tay.** Lab chỉ chứng minh được APISIX *với cấu hình này* có thay đổi request hay không. Để kết luận về gateway production cần danh sách plugin/route thật của họ, hoặc so sánh body bắt được phía sau gateway production.

## Kết quả kiểm chứng Phase 1 (2026-10-11, WSL2 Ubuntu 24.04, Docker 29.9)
- Lab chạy trên WSL2 của máy Windows (ECS bị IAM chặn tạo máy), không phải VM/ECS.
- `make probe` (endpoint pay-per-use ap-southeast-1): MaaS nhận **cả** `x-api-key` lẫn `Authorization: Bearer`; thiếu header → 400 `ModelArts.81002`; `?beta=true` OK; `glm-5.2`, `glm-5.3` OK; **`/v1/messages/count_tokens` → 404 `APIG.0101`** (không tồn tại trên MaaS).
- GLM trả `thinking` block kèm `signature` trong response.
- APISIX 3.11: tên consumer phải khớp `^[a-zA-Z0-9_]+$` (không có `-`); `proxy-rewrite headers.set` không ghi đè được `Authorization` client gửi, nên pre-function xoá nó trước.

## Tap (Phase 2)
- [LAB] `tap/tap.py` (aiohttp 3.13.5): một container, hai listener. `:9090` mode B (chỉ APISIX gọi tới, chuyển tiếp nguyên trạng); `:9091` mode C (client gọi thẳng, tap đổi key dev → key MaaS như APISIX). Bật bằng `make up MODE=B` (hoặc `MODE=C`); `MODE=A` tắt tap.
- [LAB] Mode B: APISIX gửi thêm `X-Lab-Consumer` và `X-Lab-Request-Id` (`$request_id`) cho tap; tap ghi lại rồi **xoá** trước khi gửi MaaS, nên MaaS nhận đúng các header như mode A. Header `Host` vẫn là host MaaS (`pass_host: rewrite`).
- [LAB] Forward byte-for-byte: body gửi đúng bytes đã đọc; thứ tự và kiểu chữ của header giữ nguyên; bỏ hop-by-hop (`Connection`, `Transfer-Encoding`...), `Host` và `Content-Length` được đặt lại (cùng giá trị). Response: `auto_decompress=False`, chunk được ghi ra client ngay khi nhận. Kiểm chứng bằng `make tap-test` (upstream giả, 19 kiểm tra).
- [LAB] Response trả client bằng chunked encoding thay vì `Content-Length` của upstream (nội dung giống hệt). aiohttp thêm `Date`/`Server` vào response.
- [LAB] TTFT của tap = từ lúc tap gửi request lên MaaS đến byte body đầu tiên nhận về. Với request non-stream, TTFT ≈ toàn bộ thời gian sinh câu trả lời, không so được với stream.
- [LAB] Session: header `X-Claude-Code-Session-Id` → `metadata.user_id` (`..._session_<uuid>` hoặc JSON có `session_id`) → consumer + khoảng nghỉ ≤ `TAP_SESSION_GAP_S` (mặc định 300s). [VERIFY] ở Phase 3: Claude Code thật gửi cái nào.
- [LAB] Client đóng kết nối giữa chừng: tap ghi `error.type=client_disconnected, status=499`. Qua APISIX, nginx đóng kết nối tới tap khi client bỏ đi, nên tap thấy được.
- [LAB] **APISIX 3.11 bỏ qua `include_req_body` khi có `log_format`**: body chỉ được ghi qua `$request_body`, mà nginx để rỗng khi body tràn ra file tạm (buffer mặc định 8k/16k). Lab đặt `client_body_buffer_size 32m` + `client_body_in_single_buffer on` (chỉ ảnh hưởng việc log). Đã kiểm chứng với body 623.529 bytes.

## Kết quả kiểm chứng Phase 2 (2026-10-11)
- `scripts/diff_bodies.py`: 7/7 request qua APISIX (mode B), kể cả body 623.529 bytes (~164K token), có sha256 body APISIX nhận == body tap nhận; 0 khác biệt ở `anthropic-version`, `anthropic-beta`, `user-agent`, `x-claude-code-session-id`. **Với cấu hình lab này, APISIX không đổi body hay các header đó.** (Chưa nói được gì về gateway production thật, xem mục APISIX.)
- Gửi lại y hệt body ~164K token sau vài giây: lần 1 `input_tokens=163640, cache_read=0`; lần 2 `input_tokens=56, cache_read=163584` (hit 99,97%). `cache_creation_input_tokens` luôn = 0, kể cả lần ghi cache đầu tiên. [VERIFY] MaaS có bao giờ báo `cache_creation` không; công thức hit của Phase 4 vẫn dùng mẫu số `input + cache_read + cache_creation`.

## Dashboard (xem cấu hình)
- [LAB] `make dashboard` dựng một APISIX **3.14.1** riêng (có UI tích hợp từ 3.13, cần Admin API + etcd) tại `http://localhost:9180/ui/`, rồi nạp vào đó cấu hình lab đang chạy (mode A/B tự nhận theo container tap) với **key đã che**. Đây là bản sao chỉ để xem: không nằm trên đường request, sửa trong UI không đổi gateway lab (vẫn là 3.11 standalone, cấu hình nằm trong git). Khác version 3.11 ↔ 3.14 chỉ ảnh hưởng hiển thị.

## Phase 3: Claude Code thật qua lab (2026-10-11)
- [LAB] Repo mẫu: **Reaparr** (`Reaparr/Reaparr`, nhánh `dev`, commit `3f0246356944`, GPL-3.0), .NET 10 + Nuxt 4/Vue 3 + TS, SQLite, TUnit/Vitest/Cypress; 2.400 file (1.549 `.cs`, 216 `.vue`, 290 `.ts`). Chọn vì gần stack mục tiêu (Vue + .NET 8/9/10), tiếng Anh, **chạy được** (đã build bằng `docker/Dockerfile` và chạy ở :7000) để sau còn test thêm tính năng/sửa bug/scan. Khác workload mục tiêu ở nghiệp vụ (tải media). Repo lab không chứa code Reaparr: `scripts/prepare-sample-repo.sh` clone đúng commit vào `sample-repo/`.
- [LAB] `CLAUDE.md` do lab viết (`sample/CLAUDE.md`), commit trên nhánh `lab` để `git status` sạch. `AGENTS.md` và `.agents/skills` của Reaparr giữ nguyên nhưng **không** import: chúng đòi hỏi question tool (không trả lời được ở `claude -p`), `trash`, `dotnet-test-mcp` (lab không có). Claude Code 2.1.296 không tự nạp `AGENTS.md`.
- [LAB] `scripts/run-claude.sh <dev> <A|B|C> [--background] [args]`: mỗi dev một `CLAUDE_CONFIG_DIR` riêng (`.lab-claude/<dev>`) để cấu hình/đăng nhập của người chạy không lọt vào request; đặt `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_{OPUS,SONNET}_MODEL`=main, `ANTHROPIC_DEFAULT_HAIKU_MODEL`=fast, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (trừ `--background`). Ghi mỗi lần chạy vào `logs/runs.jsonl`.
- Kiểm chứng biến môi trường: request mang `model` = `ANTHROPIC_MODEL` (glm-5.3), header `Authorization: Bearer <dev key>` được APISIX chấp nhận. Claude Code in cảnh báo `unrecognized_model` cho glm-5.3 nhưng vẫn chạy.
- Claude Code gửi: header **`X-Claude-Code-Session-Id`** và `metadata.user_id` = JSON `{"device_id","account_uuid":"","session_id"}` (cùng session id) → tap dùng header. `user-agent: claude-cli/2.1.296 (external, sdk-cli)`; `anthropic-beta` gồm `claude-code-20250219, interleaved-thinking, context-management, prompt-caching-scope-2026-01-05, ...`. Body: `system` 3 block, 20–21 tools, **3 marker `cache_control`**, path `/v1/messages?beta=true`. Không thấy gọi `count_tokens`.
- `--background` (bỏ `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`) với `claude -p`: không thấy request haiku-tier nào; danh sách tools có 21 thay vì 20. Traffic nền chủ yếu ở chế độ interactive → đo ở S7.

### Quan sát cache đầu tiên (chưa phải kết luận, cần S1/S2 để có đủ mẫu)
- Phiên 1 (13 request, 15K → 31K token): weighted hit **81,3%** (`261120/321315`). Request 1 = 5,2% (đầu phiên), request 4 = **4,4%**, request 9 = 63,8%, còn lại 88–98%.
- Phiên 2 (2 request): request 2 chỉ hit **15,8%** dù gửi ngay sau request 1.
- So body request N với N-1 (tap): ở **mọi** cặp, phần tử khác đầu tiên chỉ là marker `cache_control` dời xuống message cuối (hành vi chuẩn của Claude Code); **nội dung text trùng hoàn toàn** (3→4: 76.963 ký tự prefix chung; phiên 2, 1→2: 67.187 ký tự). Cùng một kiểu thay đổi lúc thì hit 95%, lúc thì hit 4% → miss ở request 4/9 và phiên 2 **không do phía client hay APISIX** gây ra; nghiêng về phía serving (routing sang instance khác, eviction). [VERIFY] với MaaS backend team.
- `cache_read_input_tokens` luôn là bội số của 64 (832, 896, 2944, 15872...) → nhiều khả năng cache theo block 64 token. [VERIFY]
- Lượng cache_read nhỏ ở đầu phiên (832–2.944 token) khớp với phần system prompt chung giữa các phiên.

## Deploy lên ECS (2026-10-11)
- [LAB] ECS `ap-southeast-1g` (cùng region với endpoint MaaS), Huawei Cloud EulerOS 3.0, 2 vCPU, 1,8 GB RAM + 2 GB swap, Docker CE 29.9 (repo CentOS 9) + Compose v5.6. Lab ở `/opt/maas-lab`, chạy **mode B** (`restart: unless-stopped`, Docker bật cùng máy).
- [LAB] `LAB_BIND=127.0.0.1`: APISIX/tap/dashboard chỉ nghe trên loopback; security group chỉ cần cổng 22. Key dev trên ECS là chuỗi ngẫu nhiên (`sk-lab-...`), không dùng key mẫu.
- [LAB] Dùng hằng ngày: `scripts/claude-maas.ps1` trên Windows mở `ssh -L 19080:127.0.0.1:9080` rồi chạy `claude` với `ANTHROPIC_*` chỉ cho tiến trình đó và `CLAUDE_CONFIG_DIR=%USERPROFILE%\.claude-maas` (không đụng Claude Code đăng nhập Anthropic). Cổng local 19080 vì 9080 trên Windows có thể bị dịch vụ khác chiếm (đã gặp: NahimicService).
- Kiểm chứng trên ECS: `make probe` như WSL; `tap-test` 20/20, smoke 8/8, tap-smoke 10/10 (BIG_KB=64), body APISIX == tap 7/7; Claude Code từ Windows qua tunnel → consumer `dev_duong`, tap ghi session theo header.
- Claude Code 2.1.296 cảnh báo `glm-5.3` không có trong model catalog → giả định context 200K cho auto-compact. [VERIFY] context window thật của GLM-5.x để đặt `CLAUDE_CODE_MAX_CONTEXT_TOKENS`.
