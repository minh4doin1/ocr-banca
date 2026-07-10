param(
    [ValidateSet("ocr", "user")]
    [string]$Service = "ocr",
    [int]$Port = 0
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

if ($Port -le 0) {
    $Port = if ($Service -eq "user") { 8300 } else { 8100 }
}

$localUrl = "http://127.0.0.1:$Port"
$healthUrl = if ($Service -eq "user") {
    "http://localhost:$Port/healthz"
} else {
    "http://localhost:$Port/health"
}
$healthField = if ($Service -eq "user") { "status=ok" } else { "status=healthy" }

Write-Host "=== LOCAL TUNNEL (cloudflared) ===" -ForegroundColor Cyan
Write-Host "Service    : $Service"
Write-Host "Local      : $localUrl"
Write-Host "Cloudflared: $cloudflared"

try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
    if ($Service -eq "user") {
        Write-Host "Backend    : $($health.status)" -ForegroundColor Green
    } else {
        Write-Host "Backend    : $($health.status) (engine=$($health.engine))" -ForegroundColor Green
    }
} catch {
    Write-Host "Canh bao: chua thay backend tren port $Port." -ForegroundColor Yellow
    if ($Service -eq "user") {
        Write-Host "  Chay truoc: .\ocr-release-kit\Start-UserService.ps1" -ForegroundColor Yellow
    } else {
        Write-Host "  Chay truoc: .\ocr-release-kit\Start-OcrSystem.ps1" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Tim dong chua URL: https://xxxx.trycloudflare.com" -ForegroundColor Yellow
Write-Host "Giu terminal nay mo. Ctrl+C de tat tunnel." -ForegroundColor Yellow
Write-Host "Health mong doi: $healthField" -ForegroundColor DarkGray
Write-Host ""

& $cloudflared tunnel --url $localUrl --no-autoupdate
