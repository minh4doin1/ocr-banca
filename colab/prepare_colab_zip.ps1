# Tạo file zip ocr-service để upload lên Google Colab
# Dùng Python để zip đúng path forward-slash (tương thích Linux/Colab)
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path $PSScriptRoot -Parent
$serviceRoot = Join-Path $repoRoot "ocr-service"
$outZip = Join-Path $PSScriptRoot "ocr-service.zip"
$pythonExe = Join-Path $serviceRoot "venv\Scripts\python.exe"
if (!(Test-Path $pythonExe)) { $pythonExe = "python" }

if (!(Test-Path $serviceRoot)) {
    throw "Không tìm thấy ocr-service: $serviceRoot"
}

$pyScript = @"
import zipfile
from pathlib import Path

service = Path(r'$serviceRoot')
out = Path(r'$outZip')
skip = {'__pycache__', '.pyc', 'storage', 'venv', 'bin'}

if out.exists():
    out.unlink()

with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
    req = service / 'requirements.txt'
    if req.exists():
        zf.write(req, 'requirements.txt')
    app = service / 'app'
    for f in app.rglob('*'):
        if not f.is_file():
            continue
        if any(s in f.parts for s in skip):
            continue
        arc = f.relative_to(service).as_posix()
        zf.write(f, arc)

# Verify
with zipfile.ZipFile(out) as zf:
    names = zf.namelist()
    assert 'app/main.py' in names, f'Thiếu app/main.py trong zip: {names[:5]}'
print(f'OK: {out} ({len(names)} files)')
"@

$pyScript | & $pythonExe -
if ($LASTEXITCODE -ne 0) { throw "Tạo zip thất bại" }

Write-Host "Da tao: $outZip" -ForegroundColor Green
Write-Host "Upload file nay len Colab (cell 2 trong OcrWorker.ipynb)"
