# Cài CUDA/cuDNN cho Paddle GPU trên Windows (máy host RTX 2070)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$venvPip = Join-Path $root "venv\Scripts\pip.exe"
if (!(Test-Path $venvPip)) { $venvPip = "pip" }

Write-Host "=== Kiểm tra NVIDIA GPU ===" -ForegroundColor Cyan
try {
    nvidia-smi
} catch {
    Write-Host "Không tìm thấy nvidia-smi — cài driver NVIDIA trước." -ForegroundColor Red
    exit 1
}

Write-Host "`n=== Cài nvidia-cuda/cudnn qua pip (~700MB) ===" -ForegroundColor Cyan
& $venvPip install --default-timeout=600 nvidia-cuda-runtime-cu11==11.8.89 nvidia-cublas-cu11==11.11.3.6 nvidia-cudnn-cu11==8.9.5.29
& $venvPip install "setuptools<81"

Write-Host "`n=== Cài torch CPU cho VietOCR (~200MB) ===" -ForegroundColor Cyan
# Bản CPU: tránh xung đột CUDA/pybind với paddlepaddle-gpu. Paddle vẫn chạy GPU.
& $venvPip install --default-timeout=600 torch==2.4.1+cpu torchvision==0.19.1+cpu --index-url https://download.pytorch.org/whl/cpu

Write-Host "`n=== Kiểm tra Paddle GPU ===" -ForegroundColor Cyan
$py = Join-Path $root "venv\Scripts\python.exe"
& $py -c @"
from app.services.gpu_runtime import probe_gpu_runtime
s = probe_gpu_runtime()
print(s.to_dict())
if not s.paddle_gpu_ok:
    raise SystemExit('GPU chưa sẵn sàng — xem detail ở trên')
print('GPU OK:', s.gpu_name)
"@

Write-Host "`n=== Bật GPU trong .env ===" -ForegroundColor Cyan
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    (Get-Content $envFile -Raw) -replace 'PADDLE_USE_GPU=false', 'PADDLE_USE_GPU=true' | Set-Content $envFile -Encoding UTF8
    Write-Host "Đã set PADDLE_USE_GPU=true trong .env" -ForegroundColor Green
} else {
    Write-Host "Chưa có .env — copy từ .env.example và set PADDLE_USE_GPU=true" -ForegroundColor Yellow
}

Write-Host "`nXong. Restart server: uvicorn app.main:app --host 0.0.0.0 --port 8100" -ForegroundColor Green
