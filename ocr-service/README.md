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

Attributes ghi lên Keycloak: `cccd`, `fullName`, `branchCode`, `ipcasCode`,
`phoneNumber`, `unitCode`, `agentCode`, `branchName`, `departmentName`.

## Tạo lô User theo SOP manual

Luồng tự động hoá SOP khởi tạo/phân quyền Keycloak:

| SOP | Field hệ thống | Keycloak |
| --- | --- | --- |
| Email | `email` | `username` + `email` |
| First name / Last name | `first_name`, `last_name` | `firstName`, `lastName` |
| Mã chi nhánh | `branch_code` | attribute `branchCode` |
| Mã IPCAS | `ipcas_code` | attribute `ipcasCode` |
| CCCD | `cccd` | attribute `cccd` |
| Số điện thoại | `phone` | attribute `phoneNumber` |
| Mã đơn vị | `unit_code` | attribute `unitCode` |
| Vai trò | `role` | **client role** trên `KEYCLOAK_ROLES_CLIENT_ID` |

**Client role mapping:**

| Vai trò nghiệp vụ | Role Keycloak |
| --- | --- |
| Quản trị | `banca-admin` |
| Đại lý viên | `banca-seller` |
| Kế toán viên | `banca-accounting-operator` |
| Phê duyệt viên | `banca-accounting-controller` |

- Mật khẩu mặc định: `Agribank@123` (Temporary=ON)
- Required actions: `UPDATE_PASSWORD`, `CONFIGURE_TOTP`
- User đã tồn tại: Save Details + cập nhật attributes + gán client role (password/OTP chỉ khi `on_conflict` yêu cầu)

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
- `view-clients` (tra client UUID và role definition để gán client role)

### 3. Cấu hình `.env`

```env
KEYCLOAK_BASE_URL=https://admin-sso.agribank.com
KEYCLOAK_REALM=agribank
KEYCLOAK_CLIENT_ID=user-provisioning-tool
KEYCLOAK_CLIENT_SECRET=YOUR_SECRET
KEYCLOAK_ROLES_CLIENT_ID=banca-app
KEYCLOAK_DEFAULT_TEMP_PASSWORD=Agribank@123
USER_REQUIRED_FIELDS=email,first_name,last_name,branch_code,ipcas_code,cccd,phone,unit_code,role
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
      "email": "hungphamtuan2@agribank.com.vn",
      "first_name": "Hùng",
      "last_name": "Phạm Tuấn",
      "branch_code": "1500",
      "ipcas_code": "HQPTHUNG",
      "cccd": "002409012027",
      "phone": "0982867163",
      "unit_code": "95204001",
      "role": "banca-seller",
      "on_conflict": "skip"
    }
  ]
}
```

Mật khẩu reset đặt `temporary=true` (bắt đổi lần đăng nhập kế tiếp). Reset OTP =
xóa credential `type=otp` + gán lại `CONFIGURE_TOTP` (không tự sinh/không lưu OTP
secret).

## Provision user qua user-service (khuyến nghị)

Từ giai đoạn 3, OCR service **không gọi Keycloak Admin API trực tiếp** nữa —
toàn bộ qua BE service trung gian (Node.js + Fastify, xem thư mục `../user-service/`).

### Tại sao

| Cách cũ (KeycloakClient trực tiếp) | Cách mới (qua user-service) |
|---|---|
| OCR service tự code REST API, tự resolve UUID | user-service tự resolve và cache UUID |
| Phải handle 409/404 thủ công | user-service map sẵn → exception Python rõ ràng |
| Phải retry với client khác trên 403 | user-service dùng 1 client, không cần retry |
| F5/WAF chặn `/admin/realms/*` từ bên ngoài | user-service chạy trong cluster, gọi Keycloak qua cluster DNS |
| Logic rải khắp router | Tập trung trong `user-service/src/services/` |

### Cấu hình

```env
# URL user-service — cluster DNS hoặc qua ingress
USER_SERVICE_URL=http://user-service.agribank.svc.cluster.local
# hoặc: USER_SERVICE_URL=https://api.agribank.com.vn/api/v1/users-svc

# Shared secret với user-service
USER_SERVICE_API_KEY=<cùng giá trị SERVICE_API_KEY đã set cho user-service>

# Timeout
USER_SERVICE_TIMEOUT_SECONDS=30

# Client chứa role banca-* (user-service cần biết để resolve UUID)
USER_SERVICE_ROLES_CLIENT_ID=banca-app
```

### Không cần config Keycloak cho provision

Khi `USER_SERVICE_URL` đã set, OCR service **không cần**:
- `KEYCLOAK_BASE_URL`
- `KEYCLOAK_REALM`
- `KEYCLOAK_CLIENT_ID` / `KEYCLOAK_CLIENT_SECRET`

User-service đã có config Keycloak riêng. OCR service chỉ cần gọi API cao cấp.

### Fallback

Nếu user-service không khả dụng, có thể quay lại gọi Keycloak trực tiếp bằng cách:
- Bỏ `USER_SERVICE_URL` → OCR service trả 503 với message rõ
- Hoặc giữ `KEYCLOAK_BASE_URL` cho các endpoint diagnostic (keycloak-diagnostics, keycloak-role-check) — những endpoint này vẫn dùng `KeycloakClient` trực tiếp

### Health check user-service

```bash
# Từ trong cluster
kubectl -n agribank exec deploy/ocr-banca -- \
  curl -i http://user-service/healthz
```

### Migration timeline

| Giai đoạn | Trạng thái | Mô tả |
|---|---|---|
| 1 | ✅ Done | `kc-proxy/` — bypass F5 bằng proxy mỏng (giữ làm fallback) |
| 2 | ✅ Done | `user-service/` — BE service thật, dùng `@keycloak/keycloak-admin-client` |
| 3 | ✅ Done | **OCR service refactor — gọi user-service thay cho KeycloakClient** |
| 4 | (sau) | Decommission `kc-proxy/` khi F5 đã cấu hình allow rule |

### Tests

```bash
# Unit test cho UserServiceClient (mock HTTP)
pytest tests/test_user_service_client.py -v

# Unit test cho provision logic (mock UserServiceClient)
pytest tests/test_users.py -v
```

Kết quả: 32/32 tests pass cho `test_user_service_client.py`.

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
