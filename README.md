# MaaS cache lab: Claude Code → APISIX → Huawei MaaS

Lab tái hiện luồng **Claude Code CLI → APISIX (gateway) → Huawei Cloud MaaS** (endpoint tương thích
Anthropic, model GLM) để đo prompt-cache hit theo từng request, tìm chỗ prefix bị lệch giữa các request
liên tiếp, và tách lỗi phía client/gateway khỏi miss phía serving.

```
Claude Code ──► APISIX :9080 ──► (tap :9090, mode B) ──► Huawei MaaS (glm-5.3)
                    │                     └─ claude-* (tuỳ chọn) ──► api.anthropic.com
                    └─ key-auth theo dev, xoá key dev, gắn key MaaS, file-logger
```

Giả định, quyết định thiết kế và mọi kết quả đo nằm ở [`ASSUMPTIONS.md`](ASSUMPTIONS.md); kết luận cuối ở
[`docs/findings.md`](docs/findings.md) (được ghép vào cuối `reports/report.md`).

## Cấu trúc

| Đường dẫn | Nội dung |
|---|---|
| `apisix/` | Cấu hình APISIX standalone (`config.yaml`), template route/consumer (`apisix.yaml.tmpl`), `render.py` điền key lúc khởi động |
| `tap/` | Proxy ghi lại request/response (aiohttp), test offline `test_tap.py` |
| `analyze.py` | Phase 4: `logs/tap/` → `reports/report.md` + `reports/requests.csv` |
| `scenarios/` | Phase 5: `run.py` (S1–S8), `s9.ps1` (S9 từ Windows), `prompts/` |
| `scripts/` | probe, smoke, tap-smoke, diff body, chạy Claude Code (`run-claude.sh`, `claude-maas.ps1`), repo mẫu |
| `sample/CLAUDE.md` | `CLAUDE.md` của lab cho repo mẫu |
| `dashboard/` | APISIX 3.14 + etcd chỉ để xem cấu hình (`make dashboard`) |
| `docs/findings.md` | Phần Findings + khuyến nghị |

`logs/`, `reports/`, `sample-repo*/`, `.env` không nằm trong git.

## Yêu cầu

Linux có Docker Engine + Compose v2, `make`, `git`, `curl`, Python ≥ 3.11 (chỉ thư viện chuẩn). Từ Phase 3
cần Claude Code (`curl -fsSL https://claude.ai/install.sh | bash`). Đã chạy trên Ubuntu 24.04 (WSL2) và
Huawei Cloud EulerOS 2.0/3.0 (ECS, Docker CE từ repo CentOS 9).

## Cài đặt

```bash
git clone https://github.com/vubinhduong91/maas-huawei-cloud.git && cd maas-huawei-cloud
cp .env.example .env && chmod 600 .env
# Điền: MAAS_API_KEY; DEV_KEYS (chuỗi ngẫu nhiên, tên dev chỉ gồm [A-Za-z0-9_]);
# máy có IP public: LAB_BIND=127.0.0.1 và chỉ mở cổng 22 (truy cập qua ssh -L).
```

Không bao giờ in key ra log: APISIX không ghi header, tap che `Authorization`/`x-api-key`, key MaaS chỉ
nằm trong `.env` và docker volume chứa cấu hình đã render (`make down` xoá volume).

## Chế độ (mode)

| Mode | Đường đi | Lệnh |
|---|---|---|
| A | Client → APISIX → MaaS (giống production, tắt tap) | `make up MODE=A` |
| B | Client → APISIX → tap → MaaS (ghi lại mọi request) | `make up MODE=B` |
| C | Client → tap `:9091` → MaaS (bỏ qua APISIX, baseline) | `make up MODE=C` |

`ANTHROPIC_PASSTHROUGH=1` trong `.env`: request có `model` bắt đầu bằng `claude-` đi tới
`api.anthropic.com` bằng token của chính client (ở mode B vẫn qua tap). Để `0` khi chạy kịch bản đo cache.

## Chạy từng phase

| Phase | Việc | Lệnh |
|---|---|---|
| 1 | Kiểm tra MaaS (header auth, `count_tokens`, model) | `make probe` |
| 1 | APISIX passthrough: non-stream, SSE không bị buffer, 401, không lộ key | `make up MODE=A && make smoke` |
| 2 | Test tap offline (upstream giả) | `make tap-test` |
| 2 | Đủ Phase 2 với MaaS thật + diff body APISIX vs tap | `make test-phase2` (`BIG_KB=64` để giảm token) / `make diff` |
| 3 | Repo mẫu (Reaparr, ghim commit) | `scripts/prepare-sample-repo.sh` |
| 3 | Claude Code qua lab | `scripts/run-claude.sh dev_alice B` (thêm `-p "..."` để chạy headless) |
| 4 | Test analyzer / phân tích | `make test-analyze` / `make analyze ARGS="--since 2026-10-11T10:00"` |
| 5 | Kịch bản | `make scenario S=S2`, `make scenario S="S1 S4"`, `make scenario S=all` |
| 6 | Báo cáo cuối có Findings | `make report ARGS="--since ..."` → `reports/report.md` |

Khác: `make logs`, `make render-check` (cấu hình đã render, key che), `make dashboard` (UI chỉ xem tại
`http://localhost:9180/ui/`), `make clean-logs`, `make down`.

## Kịch bản (Phase 5)

Chạy trên máy lab với lab ở mode B. Mỗi session được gắn nhãn trong `logs/scenarios.jsonl`; mỗi kịch bản
ghi thêm `reports/scenario-<S>-<run>.md`.

| ID | Làm gì |
|---|---|
| S1 | Gửi lại 5 lần một request Claude Code thật, qua APISIX và thẳng vào tap |
| S2 | Một phiên 15 turn coding trên repo mẫu (`prompts/coding-15.txt`) |
| S3 | Hai phiên mới, cùng prompt, cách nhau 60 s |
| S4 | TTL: mỗi mốc 30 s…15 phút một nonce riêng, ghi cache rồi gửi lại sau khoảng nghỉ |
| S5 | 3 dev song song, mỗi người một bản copy repo |
| S6 | Ép compaction (context 50K + turn `/compact`) |
| S7 | Bật traffic nền + đổi sang tier haiku giữa phiên |
| S8 | Cùng request có và không có `cache_control` |
| S9 | Từ Windows đã đăng nhập Claude: 5 turn với glm-5.3 vs Claude Sonnet (`scenarios/s9.ps1`, cần `ANTHROPIC_PASSTHROUGH=1`) |

**Ngân sách:** runner dừng khi chi phí MaaS ước lượng của run vượt `SCENARIO_BUDGET_USD` (mặc định 15).
Giá đặt ở `MAAS_PRICE_*_PER_M`; để trống thì dùng cận trên (giá Z.ai, cache read = giá input).

## Dùng Claude Code hằng ngày qua lab (Windows)

```powershell
scripts\claude-maas.ps1 -Server root@<ip> -IdentityFile $env:USERPROFILE\.ssh\<key>
```

Mở `ssh -L 19080 → server:9080`, đặt biến `ANTHROPIC_*` chỉ cho tiến trình đó. Mặc định: đăng nhập
subscription Claude bình thường, tier sonnet = `glm-5.3[1m]` qua MaaS, opus/haiku qua subscription
(server cần `ANTHROPIC_PASSTHROUGH=1`). `-MaasOnly`: mọi tier là GLM, xác thực bằng key dev.
`-ClaudeSonnet`: giữ Claude Sonnet thật. Key dev đọc từ `%USERPROFILE%\.claude-maas\dev-key.txt`.
Làm luân phiên trên 2 máy (cài máy mới, đồng bộ memory, đăng nhập SSH bằng password): `docs/two-pc.md`.
Ngày đầu trên máy mới, từng bước: `docs/new-pc-setup.md`.

## Đọc báo cáo (`reports/report.md`)

- **Công thức:** `cache hit = cache_read / (input + cache_read + cache_creation)`; nhóm dùng trung bình có
  trọng số theo token.
- **By model / By scenario / By session:** hit, TTFT P50/P95, phân bố nguyên nhân lệch prefix.
- **Prefix divergence:** so request N với N−1 cùng session (thứ tự `system`, `tools`,
  `messages[i].content[j]`, bỏ `cache_control`, block text = text). `append-only` nghĩa là không lệch.
  Mỗi chỗ lệch có đường dẫn (vd `system[0].text` ký tự 2.340), đoạn trích ±200 ký tự, % prefix chung và
  nguyên nhân (ngày giờ, git status, cwd, thứ tự tool, system-reminder, file inject, compaction, đổi model…).
- **Expected vs actual:** expected = prefix chung dài nhất với bất kỳ request trước (cùng model, 120 phút),
  quy ra token (xấp xỉ), làm tròn block 64. Actual thấp hơn expected > 20 điểm → **miss phía serving**
  (TTL, eviction, routing, block, `cache_control`), không phải lỗi client.
- **Estimated MaaS cost:** chỉ tính request tới MaaS.
- **Findings:** ở cuối (`docs/findings.md`).

## Lab key-order drift

Điều tra việc thứ tự key trong `tools[].input_schema.properties` (và các dict khác) đổi giữa các request cùng
session, làm giảm cache hit. Đường đo: `Claude Code → tap-pre :9001 → APISIX → tap-post (tap mode B) → MaaS`;
hai tap byte-transparent, ghi nguyên bytes vào `logs/raw/{pre,post}/`. Kết luận: `docs/keyorder-findings.md`;
chi tiết từng phase: các mục "Lab key-order drift" trong `ASSUMPTIONS.md`.

| Phase | Việc | Lệnh |
|---|---|---|
| 1 | Bật tap-pre (lab đang ở mode B) / test analyzer | `make keyorder-up` / `make test-keyorder` |
| 1 | Phân tích drift offline (4 tầng dict, ghép pre/post) | `make keyorder ARGS="--session <id> --forwarded"` |
| 2 | Session Claude Code cố định (2a built-in, 2b + MCP `lab-tfs`) | `make keyorder-session V=2b` |
| 3 | Tác động cache theo vị trí drift (gọi thẳng MaaS) | `python3 scenarios/keyorder_cache.py` |
| 4 | Profile gateway P0/P1/P2 × số worker | `bash scenarios/keyorder_phase4.sh` |
| 5 | Replay qua echo server, chỉ ghi hash (0 token) | `bash scenarios/keyorder_phase5.sh`; công cụ cho team gateway: `scenarios/replay/` |
| 6 | Kiểm chứng fix (P2S `stably_encode`, tap-post canon) | `bash scenarios/keyorder_phase6.sh` |
| 7 | Báo cáo cuối / gói bằng chứng | `make keyorder-report` → `reports/keyorder-report.md`; `make keyorder-evidence` → `reports/keyorder-evidence-<ts>.tar.gz` |

Profile gateway (`LAB_GATEWAY_PROFILE`, mặc định P0): **P0** không đụng body; **P2** pre-function decode +
`core.json.encode` (cjson); **P2S** như P2 nhưng `core.json.stably_encode`; **P1** `ai-proxy` (APISIX 3.14.1).
Script phase 4–6 tự khôi phục lab về P0 / APISIX 3.11 / tap passthrough khi xong. Gói bằng chứng gồm report,
log chạy, log hash của replay, `replay.py` và một ví dụ drift (body pre/post, `metadata.user_id` đã che); script
dừng nếu thấy bất kỳ key nào trong `.env`.
