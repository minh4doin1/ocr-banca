# OCR Release Kit

Bo nay gom 2 file de chay nhanh he thong OCR trong repo:

- `Start-OcrSystem.ps1`: script khoi dong backend OCR.
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
3. Poll health endpoint toi da 30 giay.
4. In ra cac URL:
   - Frontend OCR: `http://localhost:<port>/`
   - API docs: `http://localhost:<port>/docs`

## 5) Luu y quan trong

- He thong da co logic fallback GPU -> CPU neu thieu CUDA/cuDNN.
- Neu thay thong bao lien quan `cudnn64_8.dll`, van co the tiep tuc chay voi CPU.
- FE upload da ho tro chon `local | api | auto`.

## 6) Goi y commit

Neu ban muon commit rieng bo nay:

```bash
git add ocr-release-kit
git commit -m "add OCR release starter script and usage guide"
```
