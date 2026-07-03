# Triển khai “1 link dùng luôn” với HOST GPU 2070

Mục tiêu:
- Chỉ 1 máy **HOST GPU (RTX 2070)** chạy OCR service.
- User chỉ cần mở **1 link** để dùng, không cần setup local.

---

## Kiến trúc khuyến nghị

- Máy 2070 chạy `ocr-service` ở port `8100`.
- Publish qua Tailscale:
  - **Nội bộ tailnet (khuyên dùng):** `tailscale serve`
  - **Public internet (tuỳ chọn):** `tailscale funnel`

---

## 1) Setup host 2070 (one command)

Trong thư mục `ocr-service` trên máy host:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_host_2070.ps1 `
  -WorkerToken "agribank-ocr-2026" `
  -EnableTailscaleServe `
  -InstallAutoStart
```

Script sẽ tự:
- Tạo `venv`
- Cài dependencies + GPU runtime
- Cấu hình `.env` cho host
- Mở firewall `8100`
- Bật Tailscale Serve (link nội bộ)
- Cài auto-start khi Windows boot
- Chạy luôn OCR service

> Nếu muốn public link internet, dùng `-EnableTailscaleFunnel` thay vì `-EnableTailscaleServe`.

---

## 2) User dùng như nào

### Phương án A (khuyên dùng): user cùng tailnet
- User chỉ mở link Tailscale Serve của host (HTTPS nội bộ tailnet).
- Không cần cài repo/Python.

### Phương án B: public link
- Host bật Funnel.
- User mở public URL (HTTPS) là chạy luôn.
- Cần cân nhắc bảo mật mạng nội bộ trước khi public.

---

## 3) Script mới đã có

- `scripts/run_host_service.ps1`
  - Chạy OCR service host thủ công.
- `scripts/publish_tailscale_link.ps1`
  - Publish link qua Tailscale Serve/Funnel.
- `scripts/install_host_autostart.ps1`
  - Cài Scheduled Task tự chạy service lúc boot.
- `scripts/bootstrap_host_2070.ps1`
  - Đã nâng cấp thêm cờ:
    - `-EnableTailscaleServe`
    - `-EnableTailscaleFunnel`
    - `-InstallAutoStart`

---

## 4) Lệnh vận hành nhanh

### Bật service host
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_host_service.ps1
```

### Bật link nội bộ tailnet
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\publish_tailscale_link.ps1
```

### Bật link public (tuỳ chọn)
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\publish_tailscale_link.ps1 -Public
```

### Cài auto-start khi boot
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_host_autostart.ps1
```

---

## 5) Nếu vẫn cần mode client local (fallback)

Nếu team vẫn muốn mỗi máy chạy FE local rồi proxy về host:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_client_remote.ps1 `
  -InternalGpuUrl "http://<TAILSCALE_HOST_IP>:8100" `
  -InternalGpuToken "agribank-ocr-2026"
```

Nhưng nếu mục tiêu là “chỉ vào 1 link”, không cần bước này.

---

## 6) Checklist bàn giao cho user

- [ ] Host 2070 đã chạy service (`/health` OK)
- [ ] Tailscale Serve/Funnel đã bật
- [ ] Đã gửi đúng URL cho user
- [ ] User mở URL và upload PDF chạy được

