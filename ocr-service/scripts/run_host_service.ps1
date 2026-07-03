# Start OCR host service (GPU machine).
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\run_host_service.ps1
#   powershell -ExecutionPolicy Bypass -File .\scripts\run_host_service.ps1 -Port 8100

param(
    [string]$Host = "0.0.0.0",
    [int]$Port = 8100
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$py = ".\venv\Scripts\python.exe"
if (!(Test-Path $py)) {
    throw "Không tìm thấy venv python. Chạy bootstrap_host_2070.ps1 trước."
}

Write-Host "Starting OCR host service at http://$Host`:$Port" -ForegroundColor Cyan
& $py -m uvicorn app.main:app --host $Host --port $Port
