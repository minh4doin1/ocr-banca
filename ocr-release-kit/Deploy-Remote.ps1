# Deploy OCR tren may host (goi tu SSH hoac chay local).
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\ocr-release-kit\Deploy-Remote.ps1
#   powershell -ExecutionPolicy Bypass -File .\ocr-release-kit\Deploy-Remote.ps1 -Branch main -UseGpu

param(
    [string]$Branch = "main",
    [switch]$UseGpu = $true,
    [int]$Port = 8100,
    [switch]$StartUserService = $true,
    [int]$UserPort = 8300
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $repoRoot

Write-Host "=== OCR REMOTE DEPLOY ===" -ForegroundColor Cyan
Write-Host "Repo : $repoRoot"
Write-Host "Branch: $Branch"

git fetch origin
git checkout $Branch
git pull origin $Branch

$starter = Join-Path $repoRoot "ocr-release-kit\Start-OcrSystem.ps1"
if ($UseGpu) {
    & $starter -Port $Port
} else {
    & $starter -UseGpu:$false -Port $Port
}

if ($StartUserService) {
    $userStarter = Join-Path $repoRoot "ocr-release-kit\Start-UserService.ps1"
    & $userStarter -Port $UserPort
}
