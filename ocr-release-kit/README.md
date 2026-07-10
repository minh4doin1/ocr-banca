# OCR Release Kit

Bo nay gom cac file de chay nhanh he thong trong repo:

- `Start-OcrSystem.ps1`: script khoi dong backend OCR.
- `Start-UserService.ps1`: script khoi dong backend user-service.
- `Start-AllServices.ps1`: script khoi dong ca OCR + user-service.
- `Deploy-Remote.ps1`: git pull va khoi dong service qua SSH.
- `README.md`: huong dan su dung.

## 1) Vi tri

Folder nay duoc dat tai:

`ocr-release-kit/`

Nghia la khi push git, chi can giu nguyen cau truc repo hien tai.

## 2) Dieu kien truoc khi chay

Can co san:

- Windows + PowerShell
- Folder `ocr-service` trong cung repo
- Virtual env: `ocr-service/venv`
- Da cai dependency cho backend OCR

Neu chua co `venv`, co the setup nhanh:

```powershell
cd ocr-service
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

## 3) Cach chay script

Tu root repo:

```powershell
.\ocr-release-kit\Start-OcrSystem.ps1
```

Chay user-service:

```powershell
.\ocr-release-kit\Start-UserService.ps1
```

Chay ca OCR + user-service:

```powershell
.\ocr-release-kit\Start-AllServices.ps1
```

Quick start (khuyen dung) cho may host:

```powershell
cd C:\Projects\ocr-banca
.\ocr-release-kit\Start-AllServices.ps1
```

Chay CPU (khong dung GPU):

```powershell
.\ocr-release-kit\Start-OcrSystem.ps1 -UseGpu:$false
```

Doi port:

```powershell
.\ocr-release-kit\Start-OcrSystem.ps1 -Port 8110
```

## 4) Ket qua khi chay thanh cong

Script se:

1. Stop process cu dang lang nghe tren port OCR (neu co).
2. Start backend `uvicorn` tu `ocr-service`.
3. Poll health endpoint toi da 90 giay (GPU lan dau co the cham hon).
4. Ghi log backend vao `ocr-service/logs/uvicorn.log`.
5. In ra cac URL:
   - Frontend OCR: `http://localhost:<port>/`
   - API docs: `http://localhost:<port>/docs`

`Start-UserService.ps1` se:

1. Stop process cu tren port user-service (mac dinh 8300).
2. Build `user-service` neu chua co `dist/index.js`.
3. Start `node dist/index.js`.
4. Poll health endpoint `http://localhost:<port>/healthz`.
5. Ghi log vao `user-service/logs/user-service.log`.

## 5) Expose ra internet (local tunnel)

Can `cloudflared` (da co tren may: `winget install -e --id Cloudflare.cloudflared`).

1. Chay backend local:

```powershell
.\ocr-release-kit\Start-OcrSystem.ps1
```

2. Mo tunnel (terminal rieng, giu mo):

```powershell
.\ocr-release-kit\Start-OcrTunnel.ps1
```

Script in ra URL dang `https://xxxx.trycloudflare.com` — gui link nay cho may khac truy cap FE/API.

Tunnel cho `user-service` (port 8300):

```powershell
.\ocr-release-kit\Start-OcrTunnel.ps1 -Service user
```

Neu muon doi port:

```powershell
.\ocr-release-kit\Start-OcrTunnel.ps1 -Service user -Port 9300
```

Mo dong thoi tunnel cho ca 2 service:

```powershell
# Terminal A
.\ocr-release-kit\Start-OcrTunnel.ps1

# Terminal B
.\ocr-release-kit\Start-OcrTunnel.ps1 -Service user
```

Luu y:

- Moi lan chay tunnel = URL moi (free trycloudflare).
- Tat terminal tunnel = link het hieu luc.
- Khong dung cho du lieu nhay cam production (khong co auth mac dinh tren tunnel).

## 6) Deploy nhanh qua SSH

Chay tren may deploy:

```powershell
ssh user@host "powershell -ExecutionPolicy Bypass -File C:/Projects/ocr-banca/ocr-release-kit/Deploy-Remote.ps1"
```

Tham so hay dung:

- `-Branch main`: branch deploy.
- `-Port 8100`: port OCR service.
- `-StartUserService`: mac dinh da bat.
- `-UserPort 8300`: port user-service.

## 7) Chia se cho may khac (LAN / Tailscale)

**May user khong can cau hinh gi** — chi mo link ma may GPU host in ra.

### May GPU host (RTX 2070) — cau hinh 1 lan

```powershell
.\ocr-release-kit\Start-OcrSystem.ps1
```

Script tu ghi `PADDLE_USE_GPU=true` vao `ocr-service\.env`. Option **GPU noi bo** tu bat khi GPU san sang.

### Link chia se

| Kenh | Link vi du |
|------|------------|
| LAN | `http://192.168.x.x:8100/` |
| Tailscale | `http://100.x.x.x:8100/` |
| Internet (tunnel) | `https://xxxx.trycloudflare.com/` |

User vao link → chon **GPU noi bo** (mac dinh neu lan dau) → upload PDF.

### Kien truc 2 may (tuy chon)

Neu tach client + worker:

- **Worker GPU**: `PADDLE_USE_GPU=true`, mo port 8100
- **Client**: `.env` co `INTERNAL_GPU_URL=http://<ip-worker>:8100`

User van chi truy cap URL client, khong can biet `.env`.

## 7) Multi-user / hieu nang GPU host

May GPU host (RTX 2070) duoc toi uu cho **nhieu user cung luc**:

| Tinh nang | Mo ta |
|-----------|--------|
| **Hang doi FIFO** | Toi da 30 job cho; GPU xu ly **tuần tự** (1 worker) — tranh crash |
| **Lazy PDF** | OCR trang 1 ngay, khong doi convert het PDF |
| **Prefetch** | Poppler convert trang N+1 trong khi GPU OCR trang N |
| **Warmup** | Load model luc startup — job dau khong cho 30-60s |
| **DPI 250** | Mac dinh (nhanh hon 300, van du cho bang scan) |

**Vi sao GPU chi ~15-20%?** Pipeline hybrid tren Windows:
- **Paddle GPU**: layout + table detection (ngan)
- **VietOCR + Poppler**: chay **CPU** (bat buoc tren Windows de tranh xung dot CUDA)

=> GPU khong "full load" la binh thuong; phan lon thoi gian la CPU nhan dang chu Viet.

Cau hinh trong `ocr-service/.env`:
```
PDF_DPI=250          # 300 = cham hon, chinh xac hon
OCR_QUEUE_MAX_SIZE=30
OCR_WORKER_THREADS=1 # giu 1 tren 1 GPU
```

Kiem tra hang doi: `GET /api/ocr/queue` hoac `/health` (field `ocr_queue`).

## 8) Luu y quan trong

- He thong da co logic fallback GPU -> CPU neu thieu CUDA/cuDNN.
- Neu thay thong bao lien quan `cudnn64_8.dll`, van co the tiep tuc chay voi CPU.
- FE upload da ho tro chon `local | api | auto`.

## 9) Goi y commit

Neu ban muon commit rieng bo nay:

```bash
git add ocr-release-kit
git commit -m "add OCR release starter script and usage guide"
```
