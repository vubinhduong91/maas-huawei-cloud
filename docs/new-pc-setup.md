# Mở máy mới lần đầu: từng bước

Checklist cho ngày đầu tiên làm trên máy thứ hai. Giải thích chi tiết và xử lý sự cố: `docs/two-pc.md`.
Làm theo thứ tự; mỗi bước có dòng **Kiểm tra** để biết đã xong chưa.

## A. Tối nay, trên máy cũ (2 phút)

1. Thoát hết các cửa sổ Claude Code đang mở trong `D:\projects\spinning-seal` (`/exit`). Lúc thoát, hook
   push memory lên GitHub.

   **Kiểm tra:** mở https://github.com/vubinhduong91/spinning-seal-claude-memory/commits/main, commit mới
   nhất có dạng `memory: <tên máy> <ngày giờ>` hoặc `Claude Code memory for spinning-seal`. Nếu lúc
   thoát thấy dòng `memory-sync: ...` báo lỗi thì chạy lại `claude` một lần rồi thoát lại.

2. Repo code không còn gì chưa push:
   ```powershell
   cd D:\projects\spinning-seal
   git status -sb
   ```
   **Kiểm tra:** dòng đầu là `## main...origin/main`, không có `[ahead N]`, không có file nào bên dưới.

Không cần chép key dev: sáng mai lấy thẳng từ ECS.

## B. Sáng mai, trên máy mới

Mở **PowerShell** (không cần quyền Administrator trừ khi winget đòi).

### 1. Cài Git

```powershell
winget install --id Git.Git -e
```
Không có `winget` thì tải tại https://git-scm.com/download/win, cài mặc định.
Đóng PowerShell, mở cửa sổ mới.

**Kiểm tra:** `git --version` in ra phiên bản.

### 2. Cài Claude Code và đăng nhập

```powershell
irm https://claude.ai/install.ps1 | iex
```
Đóng PowerShell, mở cửa sổ mới, chạy `claude` **ở thư mục home** (chưa vào thư mục dự án), đăng nhập bằng
tài khoản Claude Pro, rồi `/exit`.

**Kiểm tra:** `claude --version` in ra phiên bản.

### 3. Clone code

```powershell
New-Item -ItemType Directory -Force D:\projects | Out-Null
git clone https://github.com/vubinhduong91/maas-huawei-cloud.git D:\projects\spinning-seal
```
Phải đúng đường dẫn `D:\projects\spinning-seal`.

**Kiểm tra:** `Test-Path D:\projects\spinning-seal\docs\two-pc.md` in ra `True`.

### 4. Clone memory (trước khi mở Claude Code trong dự án)

```powershell
git clone https://github.com/vubinhduong91/spinning-seal-claude-memory.git "$env:USERPROFILE\.claude\projects\D--projects-spinning-seal\memory"
```
Repo private: Git mở trình duyệt để đăng nhập GitHub, chọn đăng nhập và cho phép.
Nếu báo `already exists and is not an empty directory`: đổi tên thư mục `memory` đó thành `memory.old` rồi
clone lại.

**Kiểm tra:**
```powershell
Get-ChildItem "$env:USERPROFILE\.claude\projects\D--projects-spinning-seal\memory"
```
thấy `MEMORY.md`, `maas-cache-lab-status.md`, `two-pc-workflow.md`, ...

### 5. Lấy key dev từ ECS

Lệnh dưới SSH vào ECS (gõ mật khẩu root khi được hỏi; lần đầu gõ `yes` để nhận host key), đọc key của
`dev_duong` trong `.env` và ghi thẳng vào file, không in ra màn hình:
```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude-maas" | Out-Null
$k = ssh root@111.119.197.251 "grep '^DEV_KEYS=' /opt/maas-lab/.env | cut -d= -f2- | tr -d '\042\047' | tr ',' '\n' | grep '^dev_duong:' | cut -d: -f2-"
Set-Content -Encoding ascii -NoNewline -Path "$env:USERPROFILE\.claude-maas\dev-key.txt" -Value "$k".Trim()
```
**Kiểm tra:** `(Get-Item "$env:USERPROFILE\.claude-maas\dev-key.txt").Length` lớn hơn 0. Nếu bằng 0:
SSH vào ECS, `grep DEV_KEYS /opt/maas-lab/.env` xem tên dev, rồi chép tay key vào file bằng `notepad`.

### 6. Mở Claude Code trong dự án

```powershell
cd D:\projects\spinning-seal
claude
```
Claude Code hỏi có tin cậy thư mục này không: chọn **Yes**. Nếu hỏi riêng về hook của dự án: cũng đồng ý.

**Kiểm tra:**
- Không thấy dòng `memory-sync: ...` báo lỗi.
- Gõ: `lab MaaS đang chạy ở đâu?` Claude trả lời đúng ECS Singapore `111.119.197.251`, tức là đã đọc được
  memory.

Thoát bằng `/exit`.

### 7. Thử Claude Code qua lab

```powershell
D:\projects\spinning-seal\scripts\claude-maas.ps1 -Server root@111.119.197.251
```
Một cửa sổ ssh hiện ra: gõ mật khẩu root vào đó, **để cửa sổ mở**. Quay lại cửa sổ PowerShell, Claude
Code khởi động sau vài giây.

**Kiểm tra:**
- Dòng đầu in `claude-maas: base=http://localhost:19080 sonnet=glm-5.3 (MaaS), opus/haiku=Claude subscription ...`.
- Gõ `/model`, chọn **Sonnet**, gửi `trả lời đúng một chữ: pong`, có trả lời (đi qua GLM-5.3 trên MaaS).
- Chọn **Opus**, gửi lại câu đó, có trả lời (đi qua Claude Pro).

Thoát bằng `/exit`. Đóng cửa sổ ssh khi không dùng lab nữa.

## C. Cuối ngày, trên máy mới

```powershell
cd D:\projects\spinning-seal
git status -sb        # có thay đổi thì: git add ... ; git commit -m "..." ; git push
```
Thoát mọi cửa sổ Claude Code (memory tự push).

## D. Quay lại máy cũ

```powershell
cd D:\projects\spinning-seal
git pull
claude                # memory tự pull khi mở
```
Từ đây chỉ lặp lại C và D mỗi lần chuyển máy.
