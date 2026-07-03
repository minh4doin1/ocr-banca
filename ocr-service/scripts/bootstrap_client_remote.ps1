# Bootstrap OCR client machine that proxies OCR to internal GPU host.
# One-command setup: venv, dependencies, .env for remote worker, run server.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_client_remote.ps1 -InternalGpuUrl "http://100.x.x.x:8100" -InternalGpuToken "abc123"

param(
    [Parameter(Mandatory = $true)]
    [string]$InternalGpuUrl,

    [string]$InternalGpuToken = "",
    [string]$Host = "0.0.0.0",
    [int]$Port = 8100,
    [switch]$SkipRun
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

function Write-Step([string]$msg) {
    Write-Host "`n=== $msg ===" -ForegroundColor Cyan
}

function Ensure-EnvLine([string]$file, [string]$key, [string]$value) {
    if (!(Test-Path $file)) {
        "" | Set-Content $file -Encoding UTF8
    }
    $raw = Get-Content $file -Raw -Encoding UTF8
    $pattern = "(?m)^" + [Regex]::Escape($key) + "=.*$"
    $line = "$key=$value"
    if ($raw -match $pattern) {
        $raw = [Regex]::Replace($raw, $pattern, $line)
    } else {
        if ($raw.Length -gt 0 -and -not $raw.EndsWith("`n")) { $raw += "`r`n" }
        $raw += "$line`r`n"
    }
    Set-Content $file $raw -Encoding UTF8
}

Write-Step "Kiểm tra Python"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Không tìm thấy python trong PATH."
}
python --version

Write-Step "Tạo virtual environment (nếu chưa có)"
if (!(Test-Path ".\venv\Scripts\python.exe")) {
    python -m venv venv
}

$venvPy = ".\venv\Scripts\python.exe"
$venvPip = ".\venv\Scripts\pip.exe"

Write-Step "Cài dependencies (client)"
& $venvPy -m pip install --upgrade pip
& $venvPip install -r ".\requirements.txt"
& $venvPip install "setuptools<81"
& $venvPip install --default-timeout=600 torch==2.4.1+cpu torchvision==0.19.1+cpu --index-url https://download.pytorch.org/whl/cpu

Write-Step "Tạo/Cập nhật .env cho máy CLIENT"
$envFile = ".\.env"
if (!(Test-Path $envFile)) {
    Copy-Item ".\.env.example" $envFile
}

Ensure-EnvLine $envFile "HOST" $Host
Ensure-EnvLine $envFile "PORT" "$Port"
Ensure-EnvLine $envFile "PADDLE_USE_GPU" "false"
Ensure-EnvLine $envFile "INTERNAL_GPU_URL" $InternalGpuUrl.Trim()
Ensure-EnvLine $envFile "INTERNAL_GPU_TOKEN" $InternalGpuToken
Ensure-EnvLine $envFile "REMOTE_WORKER_TOKEN" ""
Ensure-EnvLine $envFile "REMOTE_POLL_INTERVAL_SECONDS" "1.5"
Ensure-EnvLine $envFile "REMOTE_REQUEST_TIMEOUT_SECONDS" "120"

Write-Step "Mở firewall inbound TCP $Port (nếu chưa có)"
$ruleName = "OCR Service Client $Port"
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port | Out-Null
}

Write-Host "Client đã cấu hình trỏ GPU host: $InternalGpuUrl" -ForegroundColor Green

if ($SkipRun) {
    Write-Host "`nSetup xong. Chạy server bằng lệnh:" -ForegroundColor Cyan
    Write-Host "  .\venv\Scripts\python.exe -m uvicorn app.main:app --host $Host --port $Port" -ForegroundColor White
    exit 0
}

Write-Step "Chạy OCR service client"
& $venvPy -m uvicorn app.main:app --host $Host --port $Port
