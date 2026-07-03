# OCR Worker trên Google Colab

## Bước 1 — Tạo file zip (máy Windows)

```powershell
cd d:\Agribank\banca\ocr-release-personal\colab
.\prepare_colab_zip.ps1
```

Tạo ra `colab/ocr-service.zip` (~vài MB, không gồm venv/storage/poppler Windows).

## Bước 2 — Chạy notebook Colab

1. Mở `OcrWorker.ipynb` trên Google Colab
2. **Runtime → Change runtime type → T4 GPU**
3. Run all cells
4. Cell 2: upload file `ocr-service.zip` khi được hỏi
5. Cell cuối in ra **PUBLIC URL** + **TOKEN**

## Bước 3 — FE local

1. Mở `http://localhost:8100`
2. Chọn **Google Colab**
3. Dán URL + Token → **Kiểm tra Colab** → upload PDF

## Lỗi thường gặp

| Lỗi | Cách xử lý |
|-----|------------|
| `No such file ... ocr-service` | Chưa upload zip — chạy `prepare_colab_zip.ps1` rồi upload lại cell 2 |
| `requirements.txt` not found | Zip sai cấu trúc — dùng script `prepare_colab_zip.ps1` |
| Health check DNS fail trong Colab | Bình thường — kiểm tra từ **máy local** qua FE, không gọi URL tunnel từ trong Colab |
| Tunnel URL đổi | Mỗi lần chạy lại notebook = URL mới |

## GPU nội bộ (production)

Xem `ocr-service/.env.example` — `INTERNAL_GPU_URL` + `INTERNAL_GPU_TOKEN`.
