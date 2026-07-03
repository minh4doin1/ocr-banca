# Install auto-start task for OCR host service at Windows boot.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\install_host_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File .\scripts\install_host_autostart.ps1 -TaskName "OCRHost2070"
#
# Remove:
#   schtasks /Delete /TN "OCRHost2070" /F

param(
    [string]$TaskName = "OCRHost2070"
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$runScript = Join-Path $PSScriptRoot "run_host_service.ps1"

if (!(Test-Path $runScript)) {
    throw "Không tìm thấy script: $runScript"
}

# Build command line for Task Scheduler
$psExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$taskCmd = "`"$psExe`" -NoProfile -ExecutionPolicy Bypass -File `"$runScript`""

Write-Host "Cài Scheduled Task: $TaskName" -ForegroundColor Cyan

# Create or overwrite task (run as SYSTEM at startup, highest privileges)
$create = "schtasks /Create /F /SC ONSTART /RL HIGHEST /RU SYSTEM /TN `"$TaskName`" /TR `"$taskCmd`""
cmd /c $create | Out-Host

Write-Host "`nĐã cài auto-start." -ForegroundColor Green
Write-Host "Xem task: schtasks /Query /TN `"$TaskName`" /V /FO LIST" -ForegroundColor Yellow
