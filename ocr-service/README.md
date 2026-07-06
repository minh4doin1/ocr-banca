# OCR Service — Banca Agribank

Dịch vụ OCR tiếng Việt trích xuất bảng từ PDF, phục vụ tạo lô user cho hệ thống Bancassurance.

## Tech Stack

- **Python 3.11+**
- **FastAPI** — Web framework
- **PaddleOCR** — Layout detection + Table structure recognition
- **VietOCR** — Vietnamese text recognition (Transformer)
- **pdf2image** — PDF to image conversion
- **openpyxl** — Excel export

## Cài đặt

### 1. System Dependencies

```bash
# Ubuntu/Debian
sudo apt-get install poppler-utils

# Windows — tải Poppler: https://github.com/oschwartz10612/poppler-windows/releases
# Thêm bin/ vào PATH

# macOS
brew install poppler
```

### 2. Python Dependencies

```bash
# Tạo virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# Cài đặt dependencies
pip install -r requirements.txt
```

### 3. Cấu hình

```bash
cp .env.example .env
# Chỉnh sửa .env theo môi trường
```

## Chạy ứng dụng

### Development
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8100
```

### Docker
```bash
docker-compose up -d
```

## Triển khai GPU nội bộ (RTX 2070) cho cả team

Xem hướng dẫn chi tiết:

- `DEPLOY_INTERNAL_GPU.md`

Script 1 lệnh:

- Host GPU:
  - `scripts/bootstrap_host_2070.ps1`
  - One-link Tailscale: thêm `-EnableTailscaleServe` (hoặc `-EnableTailscaleFunnel`)
- Client trỏ về host:
  - `scripts/bootstrap_client_remote.ps1`

Script vận hành host:

- `scripts/run_host_service.ps1`
- `scripts/publish_tailscale_link.ps1`
- `scripts/install_host_autostart.ps1`

## API Endpoints

| Method | Endpoint | Mô tả |
|---|---|---|
| `POST` | `/api/ocr/upload` | Upload PDF, trả về job_id |
| `GET` | `/api/ocr/status/{job_id}` | Kiểm tra trạng thái xử lý |
| `GET` | `/api/ocr/result/{job_id}` | Lấy kết quả OCR |
| `PUT` | `/api/ocr/result/{job_id}` | Cập nhật dữ liệu sau review |
| `GET` | `/api/ocr/result/{job_id}/export` | Xuất Excel |
| `GET` | `/api/ocr/jobs` | Danh sách jobs |
| `POST` | `/api/ocr/result/{job_id}/page/{page_number}/reocr` | OCR lại một trang PDF |
| `GET` | `/api/users/field-config` | Trường bắt buộc + map header OCR |
| `POST` | `/api/users/enrich` | Auto-match mã CN/ĐL + enrich theo email |
| `GET` | `/api/users/lookup/agencies` | Tra cứu chi nhánh (proxy Banca Core) |
| `GET` | `/api/users/lookup/agents` | Tra cứu đại lý (proxy Banca Core) |
| `GET` | `/api/users/preview-from-job/{job_id}` | Xem trước user map từ job OCR |
| `POST` | `/api/users/provision-batch` | Tạo lô user trên Keycloak |

## Enrich mã chi nhánh / đại lý (Banca Core)

OCR trích xuất `username`, `name`, `cccd`, tên chi nhánh, phòng GD. Mã **chi nhánh**
và **đại lý** được enrich qua Banca Core v2 (không tra CCCD).

### Cấu hình `.env`

```env
BANCA_CORE_BASE_URL=http://localhost:8996
BANCA_CORE_KEYCLOAK_REALM=agribank
BANCA_CORE_KEYCLOAK_CLIENT_ID=banca-seller
BANCA_CORE_KEYCLOAK_CLIENT_SECRET=YOUR_SECRET
BANCA_CORE_MATCH_THRESHOLD=0.88
BANCA_CORE_MATCH_SUGGEST_THRESHOLD=0.75
USER_REQUIRED_FIELDS=username,name,cccd
```

Luồng enrich (`POST /api/users/enrich`):

1. **Auto-match** tên chi nhánh + phòng GD (fuzzy tiếng Việt).
2. Nếu thiếu mã, **tra cứu đại lý theo email** (`GET /api/v1/agents/email`).
3. Người dùng có thể chọn thủ công qua lookup API hoặc UI picker trên frontend.

Attributes ghi lên Keycloak: `cccd`, `fullName`, `branchCode`, `agentCode`,
`branchName`, `departmentName`.


Module này tạo/đồng bộ user lên Keycloak 24 qua Admin REST API bằng
**Service Account** (grant `client_credentials`) — KHÔNG dùng tài khoản admin.

### 1. Tạo Service Account Client trong realm cần quản lý

- Client ID: ví dụ `user-provisioning-tool`
- `Client authentication = ON`
- `Service Accounts = ON`
- `Standard Flow = OFF`, `Direct Access Grants = OFF`

### 2. Cấp quyền (Least Privilege)

`Clients → user-provisioning-tool → Service Account Roles`, thêm role từ client
`realm-management`:

- `manage-users`
- `view-users`

### 3. Cấu hình `.env`

```env
KEYCLOAK_BASE_URL=https://keycloak-domain
KEYCLOAK_REALM=myrealm
KEYCLOAK_CLIENT_ID=user-provisioning-tool
KEYCLOAK_CLIENT_SECRET=YOUR_SECRET
```

### 4. Gọi API

Nguồn dữ liệu: `job_id` (kết quả OCR đã review) **hoặc** `users` (JSON trực tiếp).
Khi user đã tồn tại, xử lý theo `on_conflict` (per-user hoặc mặc định của lô):
`skip` | `reset_password` | `reset_otp` | `reset_both`.

```jsonc
// POST /api/users/provision-batch
{
  "job_id": "abc123",              // hoặc bỏ trống và dùng "users"
  "default_on_conflict": "skip",
  "users": [
    {
      "username": "nguyenvana",
      "name": "Nguyễn Văn A",
      "cccd": "001234567890",
      "email": "a@example.com",
      "branch_code": "001",
      "agent_code": "DL001",
      "on_conflict": "reset_both"   // ghi đè mặc định của lô cho user này
    }
  ]
}
```

Mật khẩu reset đặt `temporary=true` (bắt đổi lần đăng nhập kế tiếp). Reset OTP =
xóa credential `type=otp` + gán lại `CONFIGURE_TOTP` (không tự sinh/không lưu OTP
secret).

## Cấu trúc thư mục

```
ocr-service/
├── app/
│   ├── main.py              # FastAPI entry point
│   ├── config.py            # Settings
│   ├── routers/
│   │   ├── ocr.py           # API routes OCR
│   │   └── users.py         # Enrich + provision user
│   ├── services/
│   │   ├── pdf_service.py   # PDF → Image
│   │   ├── ocr_service.py   # OCR pipeline
│   │   ├── table_service.py # Table extraction + reocr
│   │   ├── excel_service.py # Excel export
│   │   ├── keycloak_service.py
│   │   ├── banca_core_service.py
│   │   ├── branch_agent_matcher.py
│   │   └── user_mapping.py
│   ├── models/schemas.py    # Pydantic models
│   └── utils/image_utils.py # Helpers
├── storage/                 # Runtime file storage
├── tests/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## Swagger Docs

Sau khi chạy, truy cập: `http://localhost:8100/docs`
