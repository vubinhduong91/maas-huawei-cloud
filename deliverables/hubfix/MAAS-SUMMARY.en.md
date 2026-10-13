# Claude Code on MaaS glm-5.3: effort levels cause HTTP 400, and the gateway fixes cost prompt caching

1. Claude Code (2.1.29x) sends `output_config.effort` at five levels: `low`, `medium`, `high`, `xhigh`, `max`. Its default for Sonnet is `medium`.
2. MaaS `glm-5.3` (`ap-southeast-1`, Anthropic-compatible API) accepts only `low`, `high` and `max`. `medium` and `xhigh` return HTTP 400 (table below), so every Sonnet session at the default level fails.
3. `thinking` (`adaptive`, or `enabled` with `budget_tokens`), `max_tokens` up to 128000, and an effort inside `messages[].output_config` are all accepted; the top-level effort is the only blocker.
4. To avoid the 400, customer gateways rewrite the request body. Re-encoding the JSON (e.g. with lua-cjson in APISIX) changes the key order between requests and breaks MaaS prefix caching.
5. Measured with real Claude Code sessions: cache hit 53–56% when the gateway re-encodes, 92–96% when it changes only the two values in place.
6. **Request:** accept all five Anthropic effort levels on `glm-5.3` and map them internally, e.g. `medium → high`, `xhigh → max`. Clients and gateways would then not need to touch the body.
7. Optional: let operators alias Claude model names (e.g. `claude-sonnet-*` → `glm-5.3`). Today these return 404 `ModelArts.81009 Invalid model.`, so gateways rewrite `model` too.
8. Optional: return errors in the Anthropic shape (`{"type":"error","error":{"type":"invalid_request_error",...}}`). The current body has `error.type: "BadRequest"` plus `error_code` / `error_msg` / `span_id`.
9. Data: lab on Huawei Cloud ECS Singapore, 2026-10-13. Captured Claude Code bodies were sent directly to MaaS, changing one field at a time (23 variants).

| `output_config.effort` (top level), model `glm-5.3` | HTTP | MaaS error |
|---|---:|---|
| `low`, `high`, `max`, or absent | 200 | |
| `medium`, `xhigh` | 400 | `ModelArts.81001` `request param validation error, Value error, reasoning_effort must be [low, high, max] for glm-5.3` |
| any, with model `claude-sonnet-5-5` | 404 | `ModelArts.81009` `Invalid model.` (checked before effort) |
