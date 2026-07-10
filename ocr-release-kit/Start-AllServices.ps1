param(
    [switch]$UseGpu = $true,
    [int]$OcrPort = 8100,
    [int]$UserPort = 8300
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$kitRoot = $PSScriptRoot
$ocrScript = Join-Path $kitRoot "Start-OcrSystem.ps1"
$userScript = Join-Path $kitRoot "Start-UserService.ps1"

Write-Host "=== START ALL SERVICES ===" -ForegroundColor Cyan
Write-Host "OCR port  : $OcrPort"
Write-Host "User port : $UserPort"
Write-Host ""

if (!(Test-Path $ocrScript)) {
    throw "Khong tim thay $ocrScript"
}
if (!(Test-Path $userScript)) {
    throw "Khong tim thay $userScript"
}

if ($UseGpu) {
    & $ocrScript -Port $OcrPort
} else {
    & $ocrScript -UseGpu:$false -Port $OcrPort
}

& $userScript -Port $UserPort

Write-Host ""
Write-Host "Da start xong ca OCR + user-service." -ForegroundColor Green
Write-Host "OCR         : http://localhost:$OcrPort/"
Write-Host "User service: http://localhost:$UserPort/healthz"
