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

## API Endpoints

| Method | Endpoint | Mô tả |
|---|---|---|
| `POST` | `/api/ocr/upload` | Upload PDF, trả về job_id |
| `GET` | `/api/ocr/status/{job_id}` | Kiểm tra trạng thái xử lý |
| `GET` | `/api/ocr/result/{job_id}` | Lấy kết quả OCR |
| `PUT` | `/api/ocr/result/{job_id}` | Cập nhật dữ liệu sau review |
| `GET` | `/api/ocr/result/{job_id}/export` | Xuất Excel |
| `GET` | `/api/ocr/jobs` | Danh sách jobs |

## Cấu trúc thư mục

```
ocr-service/
├── app/
│   ├── main.py              # FastAPI entry point
│   ├── config.py            # Settings
│   ├── routers/ocr.py       # API routes
│   ├── services/
│   │   ├── pdf_service.py   # PDF → Image
│   │   ├── ocr_service.py   # OCR pipeline
│   │   ├── table_service.py # Table extraction
│   │   └── excel_service.py # Excel export
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
