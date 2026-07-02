param(
    [switch]$UseGpu = $true,
    [int]$Port = 8100
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path $PSScriptRoot -Parent
$serviceRoot = Join-Path $repoRoot "ocr-service"
$pythonExe = Join-Path $serviceRoot "venv\Scripts\python.exe"
$uvicornExe = Join-Path $serviceRoot "venv\Scripts\uvicorn.exe"

if (!(Test-Path $serviceRoot)) {
    throw "Khong tim thay folder ocr-service: $serviceRoot"
}
if (!(Test-Path $pythonExe) -or !(Test-Path $uvicornExe)) {
    throw "Chua co virtual env. Hay tao ocr-service\\venv truoc."
}

Write-Host "=== OCR SYSTEM STARTER ===" -ForegroundColor Cyan
Write-Host "Repo root : $repoRoot"
Write-Host "Service   : $serviceRoot"
Write-Host "Port      : $Port"
Write-Host "Use GPU   : $UseGpu"

# Stop old listener on same port
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    Write-Host "Dang stop process cu PID=$($conn.OwningProcess) tren port $Port..." -ForegroundColor Yellow
    Stop-Process -Id $conn.OwningProcess -Force
}

if ($UseGpu) {
    $env:PADDLE_USE_GPU = "true"
} else {
    $env:PADDLE_USE_GPU = "false"
}

$healthUrl = "http://localhost:$Port/health"

Write-Host "Dang khoi dong backend..." -ForegroundColor Green
$proc = Start-Process `
    -FilePath $uvicornExe `
    -ArgumentList "app.main:app --host 0.0.0.0 --port $Port" `
    -WorkingDirectory $serviceRoot `
    -PassThru

Write-Host "Backend PID: $($proc.Id)"
Write-Host "Cho health-check..."

$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $res = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($res.status -eq "healthy") {
            $ok = $true
            break
        }
    } catch {
        # keep waiting
    }
}

if ($ok) {
    Write-Host "Khoi dong thanh cong: $healthUrl" -ForegroundColor Green
    Write-Host "Frontend OCR: http://localhost:$Port/"
    Write-Host "API docs    : http://localhost:$Port/docs"
    exit 0
}

Write-Host "Backend chua healthy sau 30s. Kiem tra log/terminal de debug." -ForegroundColor Red
exit 1
