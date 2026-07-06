# Tạo venv riêng cho VietOCR GPU (Phase 4)
# torch CUDA chạy process con — Paddle GPU giữ trong venv chính.
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$workerVenv = Join-Path $root "venv-vietocr-gpu"
$workerPy = Join-Path $workerVenv "Scripts\python.exe"
$workerPip = Join-Path $workerVenv "Scripts\pip.exe"

Write-Host "=== VietOCR GPU worker venv ===" -ForegroundColor Cyan
Write-Host "Root: $root"

if (!(Test-Path $workerPy)) {
    Write-Host "Tạo venv-vietocr-gpu…" -ForegroundColor Yellow
    python -m venv $workerVenv
}

Write-Host "`n=== Cai torch CUDA 11.8 (~2.5GB) ===" -ForegroundColor Cyan
$pipTrust = @(
    "--trusted-host", "pypi.org",
    "--trusted-host", "pypi.python.org",
    "--trusted-host", "files.pythonhosted.org",
    "--trusted-host", "download.pytorch.org"
)
& $workerPip install --default-timeout=900 @pipTrust `
    torch==2.4.1+cu118 torchvision==0.19.1+cu118 `
    --index-url https://download.pytorch.org/whl/cu118

Write-Host "`n=== Cai VietOCR deps ===" -ForegroundColor Cyan
& $workerPip install --default-timeout=600 @pipTrust `
    -r (Join-Path $root "requirements-vietocr-gpu.txt")

Write-Host "`n=== Kiểm tra CUDA trong worker ===" -ForegroundColor Cyan
$env:PYTHONPATH = $root
& $workerPy -c @"
import torch
print('torch', torch.__version__, 'cuda', torch.cuda.is_available())
if torch.cuda.is_available():
    print('gpu', torch.cuda.get_device_name(0))
else:
    raise SystemExit('CUDA không khả dụng trong worker venv')
"@

Write-Host "`n=== Smoke test worker IPC (main venv) ===" -ForegroundColor Cyan
$mainPy = Join-Path $root "venv\Scripts\python.exe"
$env:PYTHONPATH = $root
& $mainPy -c @"
import sys
sys.path.insert(0, r'$root')
from app.services.vietocr_gpu_client import warmup_vietocr_gpu_worker, shutdown_vietocr_gpu_client
ok = warmup_vietocr_gpu_worker()
print('warmup', ok)
shutdown_vietocr_gpu_client()
if not ok:
    raise SystemExit(1)
"@

Write-Host "`n=== Cập nhật .env ===" -ForegroundColor Cyan
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    $raw = Get-Content $envFile -Raw
    if ($raw -notmatch 'VIETOCR_GPU_SUBPROCESS') {
        Add-Content $envFile "`nVIETOCR_GPU_SUBPROCESS=true`n"
    } else {
        $raw = $raw -replace 'VIETOCR_GPU_SUBPROCESS=false', 'VIETOCR_GPU_SUBPROCESS=true'
        Set-Content $envFile $raw -Encoding UTF8
    }
    Write-Host "VIETOCR_GPU_SUBPROCESS=true" -ForegroundColor Green
}

Write-Host "`nXong. Restart server để dùng VietOCR GPU subprocess." -ForegroundColor Green
