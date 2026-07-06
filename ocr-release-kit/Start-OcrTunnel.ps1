param(
    [int]$Port = 8100
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Find-Cloudflared {
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $candidates = @(
        (Join-Path $env:ProgramFiles "cloudflared\cloudflared.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "cloudflared\cloudflared.exe")
    )

    foreach ($path in $candidates) {
        if (Test-Path $path) {
            return $path
        }
    }

    throw "Khong tim thay cloudflared. Cai: winget install -e --id Cloudflare.cloudflared"
}

$cloudflared = Find-Cloudflared
$localUrl = "http://127.0.0.1:$Port"
$healthUrl = "http://localhost:$Port/health"

Write-Host "=== OCR LOCAL TUNNEL (cloudflared) ===" -ForegroundColor Cyan
Write-Host "Local      : $localUrl"
Write-Host "Cloudflared: $cloudflared"

try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
    Write-Host "Backend    : $($health.status) (engine=$($health.engine))" -ForegroundColor Green
} catch {
    Write-Host "Canh bao: chua thay backend tren port $Port." -ForegroundColor Yellow
    Write-Host "  Chay truoc: .\ocr-release-kit\Start-OcrSystem.ps1" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Tim dong chua URL: https://xxxx.trycloudflare.com" -ForegroundColor Yellow
Write-Host "Giu terminal nay mo. Ctrl+C de tat tunnel." -ForegroundColor Yellow
Write-Host ""

& $cloudflared tunnel --url $localUrl --no-autoupdate
