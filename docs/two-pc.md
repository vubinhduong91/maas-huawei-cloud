# Làm việc trên 2 máy Windows

Dự án được làm luân phiên trên 2 PC Windows. Lab (APISIX + tap + MaaS) chạy trên ECS Singapore
`111.119.197.251` nên máy nào cũng dùng chung một lab; mỗi máy chỉ cần code, memory của Claude Code
và key dev.

## Cái gì nằm ở đâu

| Thứ | Ở đâu | Đồng bộ thế nào |
|---|---|---|
| Code | `D:\projects\spinning-seal` (repo public `maas-huawei-cloud`) | `git pull` / `git push` bằng tay |
| Memory Claude Code | `%USERPROFILE%\.claude\projects\D--projects-spinning-seal\memory` (repo **private** `spinning-seal-claude-memory`) | Tự động bằng hook khi mở/đóng phiên |
| Key dev | `%USERPROFILE%\.claude-maas\dev-key.txt` (1 dòng) | Chép tay một lần, không bao giờ đưa vào git |
| Lab, log tap, báo cáo | ECS `/opt/maas-lab` (`logs/tap`, `reports/`) | Không cần: dùng chung |
| Lịch sử phiên Claude Code (`*.jsonl`) | `%USERPROFILE%\.claude\projects\D--projects-spinning-seal\` | Không đồng bộ (mỗi máy có lịch sử riêng) |

Repo phải nằm đúng ở `D:\projects\spinning-seal` trên cả 2 máy: Claude Code tìm memory theo đường dẫn
dự án, đổi đường dẫn là mất memory.

## Cài máy mới (một lần)

1. **Cài công cụ** (PowerShell):
   ```powershell
   winget install Git.Git                 # có Git Bash, hook cần nó
   irm https://claude.ai/install.ps1 | iex   # Claude Code
   ```
   Mở cửa sổ PowerShell mới rồi chạy `claude`, gõ `/login`, đăng nhập bằng tài khoản Claude Pro.

2. **Lấy code và memory:**
   ```powershell
   git clone https://github.com/vubinhduong91/maas-huawei-cloud.git D:\projects\spinning-seal
   git clone https://github.com/vubinhduong91/spinning-seal-claude-memory.git "$env:USERPROFILE\.claude\projects\D--projects-spinning-seal\memory"
   ```
   Repo memory là private: lần clone đầu Git sẽ mở cửa sổ đăng nhập GitHub.

3. **Key dev:** mở file trên máy cũ, chép nội dung (1 dòng) sang máy mới:
   ```powershell
   New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude-maas" | Out-Null
   notepad "$env:USERPROFILE\.claude-maas\dev-key.txt"
   ```
   Không gửi key qua chat/email công khai.

4. **Thử:**
   ```powershell
   cd D:\projects\spinning-seal
   claude
   ```
   Lần đầu Claude Code hỏi có tin cậy thư mục và hook của dự án không: chọn đồng ý. Nếu thấy dòng
   `memory-sync: ...` thì xem mục Xử lý sự cố.

## Dùng hằng ngày

**Làm việc trên repo này (code, phân tích):**
```powershell
cd D:\projects\spinning-seal
git pull
claude
```
Hết việc: commit + `git push` phần code, rồi thoát Claude Code (`/exit` hoặc Ctrl+C hai lần). Memory tự
được push khi thoát.

**Dùng Claude Code qua lab (sonnet = GLM-5.3 qua MaaS, opus/haiku = Claude Pro):**
```powershell
# Máy cũ (có SSH key):
D:\projects\spinning-seal\scripts\claude-maas.ps1 -Server root@111.119.197.251 -IdentityFile $env:USERPROFILE\.ssh\maas_ecs
# Máy mới (password):
D:\projects\spinning-seal\scripts\claude-maas.ps1 -Server root@111.119.197.251
```
Với password: một cửa sổ ssh hiện ra, gõ mật khẩu root vào đó (có 2 phút) và **để nguyên cửa sổ mở**:
đóng nó là mất tunnel. Lần sau chạy lại script khi cửa sổ còn mở thì không phải gõ lại.

Tuỳ chọn thêm: `-MaasOnly` (mọi tier là GLM, không dùng Claude Pro), `-ClaudeSonnet` (giữ Claude
Sonnet thật), và mọi tham số khác chuyển thẳng cho `claude` (vd `-p "giải thích repo này"`).

## Chuyển sang máy kia

1. Máy đang dùng: `git push` code, thoát Claude Code (memory tự push).
2. Máy kia: `git pull` code, mở Claude Code (memory tự pull).

Nên thoát Claude Code ở máy này trước khi mở ở máy kia. Nếu mở cả hai cùng lúc, memory chỉ khớp lại khi
một bên thoát, và sửa cùng một dòng ở hai nơi có thể gây conflict.

## Hook đồng bộ memory

`.claude/settings.json` chạy `scripts/memory-sync.sh` khi mở phiên (mới hoặc resume) và khi thoát. Script
commit thay đổi trong thư mục memory, `git pull --rebase`, rồi `git push`. Thành công thì im lặng; lỗi thì
hiện một dòng `memory-sync: ...` và phiên vẫn chạy. Xem hoặc tắt hook: gõ `/hooks` trong Claude Code.

Đồng bộ bằng tay (khi cần), trong cửa sổ **Git Bash**:
```bash
bash /d/projects/spinning-seal/scripts/memory-sync.sh
```

## Xử lý sự cố

| Triệu chứng | Cách xử lý |
|---|---|
| `memory-sync: ... is not a git repo` | Chưa clone memory, làm lại bước 2 của phần Cài máy mới. Nếu thư mục đã có file (Claude Code tự tạo), đổi tên nó rồi clone lại. |
| `memory-sync: push failed (offline?)` | Mất mạng hoặc chưa đăng nhập GitHub. Thay đổi đã commit cục bộ, lần mở phiên sau tự push. |
| `memory-sync: pull failed (offline or conflict)` | Nếu có mạng thì là conflict: vào thư mục memory, `git pull --rebase`, sửa file bị conflict, `git add -A`, `git rebase --continue`, `git push`. Hoặc nhờ Claude Code làm. |
| `SSH tunnel did not come up` | Sai password, hết 2 phút chưa gõ, hoặc ECS không truy cập được. Chạy lại script. |
| `localhost:19080 is used by another program` | Thêm `-LocalPort 19081` (hoặc cổng trống khác). |
| `Missing ...dev-key.txt` | Làm bước 3 của phần Cài máy mới. |
| HTTP 401 từ gateway | Key dev sai hoặc thừa ký tự; file chỉ được có đúng 1 dòng key. |

## Giới hạn

- `scenarios/s9.ps1` cần SSH key (`maas_ecs`), chỉ chạy được trên máy cũ.
- Lịch sử phiên cũ không chuyển sang máy mới; `/resume` chỉ thấy phiên của máy đó.
- Muốn thống kê riêng theo máy: thêm một dev mới vào `DEV_KEYS` trong `.env` trên ECS, rồi
  `make up MODE=B` lại; mỗi máy dùng key riêng.
