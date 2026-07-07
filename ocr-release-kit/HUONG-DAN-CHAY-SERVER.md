# Hướng dẫn chạy OCR Server & chia sẻ link

Tài liệu này dành cho **máy host GPU** (RTX 2070). User chỉ cần mở link — không cài gì thêm.

---

## 1. Chuẩn bị (chỉ làm 1 lần)

### 1.1 Cài backend cơ bản

```powershell
cd C:\Projects\ocr-banca\ocr-service
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
.\venv\Scripts\pip install -r requirements-gpu.txt
.\scripts\setup_gpu_windows.ps1
```

Script trên cài cuDNN + torch CPU (process chính) và bật `PADDLE_USE_GPU=true` trong `.env`.

### 1.2 (Khuyến nghị) Phase 4 — VietOCR chạy GPU process riêng

```powershell
cd C:\Projects\ocr-banca\ocr-service
powershell -ExecutionPolicy Bypass -File .\scripts\setup_vietocr_gpu_worker.ps1
```

Tạo `venv-vietocr-gpu` (torch cu118). Trong `.env` cần có:

```
VIETOCR_GPU_SUBPROCESS=true
OCR_WORKER_THREADS=2
OCR_PAGE_PIPELINE=true
```

---

## 2. Chạy server

### Cách A — Script tự động (khuyến nghị)

Tự **tắt process cũ** trên port 8100, start server nền, in link LAN/Tailscale:

```powershell
cd C:\Projects\ocr-banca
.\ocr-release-kit\Start-OcrSystem.ps1
```

Tùy chọn:

```powershell
# CPU only
.\ocr-release-kit\Start-OcrSystem.ps1 -UseGpu:$false

# Port khác
.\ocr-release-kit\Start-OcrSystem.ps1 -Port 8110
```

Log server: `ocr-service\logs\uvicorn.log`

Xem log realtime:

```powershell
Get-Content C:\Projects\ocr-banca\ocr-service\logs\uvicorn.log -Wait -Tail 50
```

### Cách B — Chạy tay (terminal hiện log)

```powershell
cd C:\Projects\ocr-banca\ocr-service
.\venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --port 8100
```

**Lưu ý:** Nếu báo lỗi `port 8100 already in use` → server **đã chạy rồi**, không cần start lại.

Kiểm tra nhanh:

```powershell
Invoke-RestMethod http://localhost:8100/health
```

Tắt server cũ (khi cần restart):

```powershell
netstat -ano | findstr :8100
taskkill /PID <PID> /F
```

Hoặc dùng `Start-OcrSystem.ps1` — script tự kill process cũ.

---

## 3. Các loại link chia sẻ

Sau khi server chạy, port mặc định **8100**.

| Loại | Ai dùng được | Link mẫu |
|------|--------------|----------|
| **Localhost** | Chỉ máy host | `http://localhost:8100/` |
| **LAN nội bộ** | Máy cùng Wi‑Fi / mạng văn phòng | `http://192.168.x.x:8100/` |
| **Tailscale** | Máy trong tailnet (VPN mesh) | `http://100.x.x.x:8100/` |
| **Public internet** | Bất kỳ ai có link (qua tunnel) | `https://xxxx.trycloudflare.com/` |

User vào link → upload PDF → chọn **GPU nội bộ** (tự bật nếu host có GPU).

---

## 4. Lấy link LAN nội bộ

### Tự động (khi dùng Start-OcrSystem.ps1)

Script in sẵn dòng `LAN : http://192.168.x.x:8100/`.

### Thủ công

```powershell
# Liệt kê IP LAN (bỏ 127.0.0.1)
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -ne "127.0.0.1" -and $_.IPAddress -notlike "169.254.*" } |
  Select-Object IPAddress, InterfaceAlias
```

Ví dụ máy host: **`http://192.168.50.1:8100/`**

**Yêu cầu:** Máy user và máy host **cùng mạng LAN**; Windows Firewall cho phép inbound port **8100** (hoặc tạm tắt firewall private network khi test).

Mở firewall (chạy PowerShell **Admin**):

```powershell
New-NetFirewallRule -DisplayName "OCR Banca 8100" -Direction Inbound -Protocol TCP -LocalPort 8100 -Action Allow
```

---

## 5. Lấy link Tailscale

### Cài Tailscale (1 lần)

1. Tải: https://tailscale.com/download/windows  
2. Đăng nhập cùng tài khoản trên **máy host** và **máy user**.

### Lấy IP Tailscale của máy host

```powershell
tailscale ip -4
```

Ví dụ: `100.64.12.34` → link: **`http://100.64.12.34:8100/`**

Gửi link này cho user đã cài Tailscale — **không cần cùng Wi‑Fi**, an toàn hơn mở port router.

### Kiểm tra

Trên máy user (đã cài Tailscale):

```powershell
curl http://100.x.x.x:8100/health
```

---

## 6. Link public ra internet (Cloudflare Tunnel)

Dùng khi user **không cùng LAN** và **không có Tailscale**. Link dạng `https://xxxx.trycloudflare.com`.

### Bước 1 — Cài cloudflared (1 lần)

```powershell
winget install -e --id Cloudflare.cloudflared
```

### Bước 2 — Chạy backend (terminal 1)

```powershell
cd C:\Projects\ocr-banca
.\ocr-release-kit\Start-OcrSystem.ps1
```

### Bước 3 — Mở tunnel (terminal 2, giữ mở)

```powershell
cd C:\Projects\ocr-banca
.\ocr-release-kit\Start-OcrTunnel.ps1
```

Đợi vài giây, tìm dòng:

```
https://random-words.trycloudflare.com
```

Gửi link đó cho user. **Mỗi lần chạy tunnel = URL mới.** Tắt terminal tunnel → link hết hiệu lực.

### Chạy tunnel thủ công

```powershell
cloudflared tunnel --url http://127.0.0.1:8100 --no-autoupdate
```

### Lưu ý bảo mật

- Tunnel free **không có mật khẩu** — ai có link đều upload được.
- Chỉ dùng cho nội bộ / demo; không dùng production nhạy cảm.
- Có thể đặt `REMOTE_WORKER_TOKEN` trong `.env` nếu cần bảo vệ worker (nâng cao).

---

## 7. Quy trình hàng ngày (tóm tắt)

```powershell
# 1) Start server (1 terminal)
cd C:\Projects\ocr-banca
.\ocr-release-kit\Start-OcrSystem.ps1

# 2a) Chia sẻ LAN / Tailscale — copy link script in ra

# 2b) HOẶC public internet — terminal thứ 2:
.\ocr-release-kit\Start-OcrTunnel.ps1
```

Kiểm tra server OK:

| Endpoint | Mục đích |
|----------|----------|
| `http://localhost:8100/` | Giao diện upload |
| `http://localhost:8100/health` | GPU, queue, trạng thái |
| `http://localhost:8100/docs` | API Swagger |

Health tốt khi `status: "healthy"`, `gpu_available: true`. Phase 4 bật thì có thêm `vietocr_gpu_subprocess: true`.

---

## 8. Xử lý lỗi thường gặp

| Triệu chứng | Nguyên nhân | Cách xử lý |
|-------------|-------------|------------|
| `Errno 10048` port 8100 | Server đã chạy | Dùng link hiện có, hoặc `taskkill` PID cũ |
| User LAN không vào được | Firewall chặn | Mở rule port 8100 (mục 4) |
| `vietocr_gpu_ready: false` | Chưa cài worker GPU | Chạy `setup_vietocr_gpu_worker.ps1` + restart |
| GPU không sẵn sàng | Thiếu cuDNN | `.\scripts\setup_gpu_windows.ps1` |
| Tunnel không có URL | Backend chưa chạy | Start server trước, rồi chạy tunnel |

---

## 9. Script tham chiếu nhanh

| Script | Việc làm |
|--------|----------|
| `ocr-release-kit\Start-OcrSystem.ps1` | Start/stop server, in link LAN + Tailscale |
| `ocr-release-kit\Start-OcrTunnel.ps1` | Public link qua Cloudflare |
| `ocr-service\scripts\setup_gpu_windows.ps1` | Cài GPU Paddle + torch CPU |
| `ocr-service\scripts\setup_vietocr_gpu_worker.ps1` | Phase 4 VietOCR GPU |
| `ocr-service\scripts\test_phase4_vietocr_gpu.py` | Test OCR trang 2 + benchmark |

---

## 10. SSH deploy từ máy khác

Máy host **đã có OpenSSH Server** (`sshd` port **22**). Cần thêm **public key** máy deploy (user thuộc nhóm Administrators dùng file đặc biệt, không phải `~/.ssh/authorized_keys`).

### 10.1 Thiết lập trên máy host (1 lần, chạy **Run as Administrator**)

Lấy public key trên máy deploy (Linux/macOS):

```bash
cat ~/.ssh/id_ed25519.pub
# hoặc tạo mới: ssh-keygen -t ed25519 -C "deploy@ci"
```

Trên máy host Windows:

```powershell
cd C:\Projects\ocr-banca
powershell -ExecutionPolicy Bypass -File .\ocr-release-kit\Setup-SshDeploy.ps1 -PublicKey "ssh-ed25519 AAAA... deploy@ci"
```

Script sẽ: bật `sshd`, mở firewall port 22, ghi key vào `C:\ProgramData\ssh\administrators_authorized_keys`.

### 10.2 Kết nối SSH

| Kênh | Lệnh (user hiện tại: `lamanhzuto2k`) |
|------|--------------------------------------|
| **Tailscale** (khuyến nghị) | `ssh lamanhzuto2k@100.124.180.55` |
| LAN | `ssh lamanhzuto2k@<IP-LAN>` |
| Hostname | `ssh lamanhzuto2k@DESKTOP-20OUIGH` |

### 10.3 Deploy từ máy khác (1 lệnh)

```bash
ssh lamanhzuto2k@100.124.180.55 \
  "powershell -ExecutionPolicy Bypass -File C:/Projects/ocr-banca/ocr-release-kit/Deploy-Remote.ps1"
```

Hoặc SSH vào rồi chạy tay:

```powershell
cd C:\Projects\ocr-banca
git pull
.\ocr-release-kit\Start-OcrSystem.ps1
```

### 10.4 Lưu ý bảo mật

- Ưu tiên **SSH key**, tắt password nếu có thể (`PasswordAuthentication no` trong `C:\ProgramData\ssh\sshd_config`, restart `sshd`).
- Chỉ mở port 22 qua **Tailscale** hoặc LAN nội bộ; tránh expose port 22 ra internet công cộng.
- User deploy nên có quyền `git pull` vào `C:\Projects\ocr-banca`.
