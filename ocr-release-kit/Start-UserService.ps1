param(
    [int]$Port = 8300
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path $PSScriptRoot -Parent
$serviceRoot = Join-Path $repoRoot "user-service"
$packageJson = Join-Path $serviceRoot "package.json"
$distIndex = Join-Path $serviceRoot "dist\index.js"
$nodeModules = Join-Path $serviceRoot "node_modules"
$tscCmd = Join-Path $serviceRoot "node_modules\.bin\tsc.cmd"
$logDir = Join-Path $serviceRoot "logs"
$logFile = Join-Path $logDir "user-service.log"
$healthUrl = "http://localhost:$Port/healthz"
$maxWaitSec = 90

if (!(Test-Path $serviceRoot)) {
    throw "Khong tim thay folder user-service: $serviceRoot"
}
if (!(Test-Path $packageJson)) {
    throw "Khong tim thay package.json trong user-service."
}

Write-Host "=== USER SERVICE STARTER ===" -ForegroundColor Cyan
Write-Host "Repo root : $repoRoot"
Write-Host "Service   : $serviceRoot"
Write-Host "Port      : $Port"

# Stop old listener on same port
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    Write-Host "Dang stop process cu PID=$($conn.OwningProcess) tren port $Port..." -ForegroundColor Yellow
    Stop-Process -Id $conn.OwningProcess -Force
}

if (!(Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

if (!(Test-Path $nodeModules) -or !(Test-Path $tscCmd)) {
    Write-Host "Chua co dependencies cho user-service. Dang chay npm install..." -ForegroundColor Yellow
    Push-Location $serviceRoot
    try {
        npm install
    } finally {
        Pop-Location
    }
}

if (!(Test-Path $distIndex)) {
    Write-Host "Chua co dist/index.js. Dang build user-service..." -ForegroundColor Yellow
    Push-Location $serviceRoot
    try {
        npm run build
    } finally {
        Pop-Location
    }
}

Write-Host "Dang khoi dong user-service..." -ForegroundColor Green
Write-Host "Log file  : $logFile"

$proc = Start-Process `
    -FilePath "cmd.exe" `
    -ArgumentList "/c", "node dist/index.js" `
    -WorkingDirectory $serviceRoot `
    -RedirectStandardError $logFile `
    -WindowStyle Hidden `
    -PassThru

Write-Host "User-service PID: $($proc.Id)"
Write-Host "Cho health-check (toi da ${maxWaitSec}s)..."

$ok = $false
$lastErr = ""
for ($i = 0; $i -lt $maxWaitSec; $i++) {
    if ($i -gt 0) { Start-Sleep -Seconds 1 }
    try {
        $res = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5
        if ($res.status -eq "ok") {
            $ok = $true
            break
        }
    } catch {
        $lastErr = $_.Exception.Message
    }
}

if ($ok) {
    Write-Host "Khoi dong thanh cong: $healthUrl" -ForegroundColor Green
    Write-Host "API user-service: http://localhost:$Port/"
    Write-Host "Xem log         : Get-Content `"$logFile`" -Wait -Tail 50"
    exit 0
}

Write-Host "User-service chua healthy sau ${maxWaitSec}s." -ForegroundColor Red
if ($lastErr) {
    Write-Host "Loi health-check: $lastErr" -ForegroundColor Yellow
}
Write-Host "Process van co the dang chay (PID $($proc.Id)). Xem log:" -ForegroundColor Yellow
Write-Host "  Get-Content `"$logFile`" -Tail 50"
if (Test-Path $logFile) {
    Write-Host "--- Log (50 dong cuoi) ---" -ForegroundColor DarkYellow
    Get-Content $logFile -Tail 50 -ErrorAction SilentlyContinue
}
exit 1
