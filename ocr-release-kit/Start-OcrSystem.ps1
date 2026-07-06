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

function Ensure-EnvLine([string]$file, [string]$key, [string]$value) {
    if (!(Test-Path $file)) {
        Copy-Item (Join-Path $serviceRoot ".env.example") $file -ErrorAction SilentlyContinue
        if (!(Test-Path $file)) {
            "" | Set-Content $file -Encoding UTF8
        }
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

$envFile = Join-Path $serviceRoot ".env"
Ensure-EnvLine $envFile "PORT" "$Port"
Ensure-EnvLine $envFile "PADDLE_USE_GPU" ($(if ($UseGpu) { "true" } else { "false" }))
Ensure-EnvLine $envFile "PDF_DPI" "250"
Ensure-EnvLine $envFile "PDF_LAZY_CONVERT" "true"
Ensure-EnvLine $envFile "PDF_PREFETCH_PAGES" "true"
Ensure-EnvLine $envFile "OCR_WARMUP_ON_STARTUP" "true"
Ensure-EnvLine $envFile "OCR_QUEUE_MAX_SIZE" "30"
$popplerBin = Join-Path $serviceRoot "bin\poppler-24.08.0\Library\bin"
if (Test-Path $popplerBin) {
    Ensure-EnvLine $envFile "POPPLER_PATH" ($popplerBin -replace '\\', '/')
}

$healthUrl = "http://localhost:$Port/health"
$logDir = Join-Path $serviceRoot "logs"
$logFile = Join-Path $logDir "uvicorn.log"
$maxWaitSec = 90

if (!(Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

Write-Host "Dang khoi dong backend..." -ForegroundColor Green
Write-Host "Log file  : $logFile"

# Uvicorn ghi log ra stderr; chi redirect stderr (PS khong cho trung file stdout/stderr).
$proc = Start-Process `
    -FilePath $uvicornExe `
    -ArgumentList "app.main:app --host 0.0.0.0 --port $Port" `
    -WorkingDirectory $serviceRoot `
    -RedirectStandardError $logFile `
    -WindowStyle Hidden `
    -PassThru

Write-Host "Backend PID: $($proc.Id)"
Write-Host "Cho health-check (toi da ${maxWaitSec}s, GPU lan dau co the cham)..."

$ok = $false
$lastRes = $null
$lastErr = ""

for ($i = 0; $i -lt $maxWaitSec; $i++) {
    if ($i -gt 0) {
        Start-Sleep -Seconds 1
    }
    try {
        $res = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5
        $lastRes = $res
        if ($res.status -eq "healthy") {
            $ok = $true
            break
        }
        if (($i + 1) % 10 -eq 0) {
            $gpuDetail = if ($res.gpu.detail) { $res.gpu.detail } else { "n/a" }
            Write-Host "  ... ${i}s status=$($res.status) gpu_available=$($res.gpu_available) ($gpuDetail)" -ForegroundColor DarkYellow
        }
    } catch {
        $lastErr = $_.Exception.Message
        if (($i + 1) % 10 -eq 0) {
            Write-Host "  ... ${i}s chua phan hoi ($lastErr)" -ForegroundColor DarkGray
        }
    }
}

if ($ok) {
    Write-Host "Khoi dong thanh cong: $healthUrl" -ForegroundColor Green
    Write-Host "Frontend OCR: http://localhost:$Port/"
    Write-Host "API docs    : http://localhost:$Port/docs"
    Write-Host "Xem log     : Get-Content `"$logFile`" -Wait -Tail 50"
    try {
        $tsIp = (tailscale ip -4 2>$null | Select-Object -First 1).Trim()
        if ($tsIp) {
            Write-Host "Tailscale   : http://${tsIp}:$Port/" -ForegroundColor Cyan
        }
    } catch {}
    $lanIps = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike "169.254.*" -and $_.IPAddress -ne "127.0.0.1" } |
        Select-Object -ExpandProperty IPAddress -Unique
    if ($lanIps) {
        Write-Host "LAN         : http://$($lanIps[0]):$Port/" -ForegroundColor Cyan
    }
    if ($UseGpu) {
        Write-Host 'Chia se link tren cho user - chon GPU noi bo, khong can cau hinh them.' -ForegroundColor Green
    }
    exit 0
}

Write-Host "Backend chua healthy sau ${maxWaitSec}s." -ForegroundColor Red
if ($lastRes) {
    Write-Host "Health cuoi: status=$($lastRes.status) gpu_available=$($lastRes.gpu_available) detail=$($lastRes.gpu.detail)" -ForegroundColor Yellow
} elseif ($lastErr) {
    Write-Host "Loi health-check: $lastErr" -ForegroundColor Yellow
}
Write-Host "Process van co the dang chay (PID $($proc.Id)). Xem log:" -ForegroundColor Yellow
Write-Host "  Get-Content `"$logFile`" -Tail 40"
if (Test-Path $logFile) {
    Write-Host '--- Log (40 dong cuoi) ---' -ForegroundColor DarkYellow
    Get-Content $logFile -Tail 40 -ErrorAction SilentlyContinue
}
exit 1
