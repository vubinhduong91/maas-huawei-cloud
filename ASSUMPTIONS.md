# Assumptions & cần xác minh

Ký hiệu: **[DOC]** có trong tài liệu Huawei, **[VERIFY]** cần kiểm chứng bằng lab hoặc hỏi MaaS/khách hàng, **[LAB]** quyết định thiết kế của lab.

## Môi trường
- [LAB] Lab chạy trên Linux VM/ECS (Docker Compose v2, Python 3.11+, bash, curl). Code được viết trên Windows; `.gitattributes` ép LF.
- [VERIFY] Claude Code trên máy viết code: **2.1.296**. Phiên bản trên VM chạy lab phải ghi lại ở Phase 3 (`claude --version`).

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
- [LAB] `file-logger` dùng `log_format` tường minh, **không ghi request header** (vì sau `proxy-rewrite` header chứa key MaaS). Body ghi qua `$request_body` với `max_req_body_bytes` 16 MiB. [VERIFY] ở Phase 2: body log khớp sha256 với body tap nhận được cho request lớn (~160K tokens).
- [LAB] TLS tới MaaS: không bật verify cert upstream (mặc định nginx). Chỉ dùng cho lab.
- [VERIFY] **Cấu hình APISIX thật của gateway production không có trong tay.** Lab chỉ chứng minh được APISIX *với cấu hình này* có thay đổi request hay không. Để kết luận về gateway production cần danh sách plugin/route thật của họ, hoặc so sánh body bắt được phía sau gateway production.
