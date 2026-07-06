# Bootstrap OCR host GPU (RTX 2070) on Windows.
# One-command setup: venv, dependencies, GPU runtime, .env, firewall, run server.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_host_2070.ps1
#   powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_host_2070.ps1 -WorkerToken "abc123" -Port 8100

param(
    [string]$Host = "0.0.0.0",
    [int]$Port = 8100,
    [string]$WorkerToken = "",
    [switch]$EnableTailscaleServe,
    [switch]$EnableTailscaleFunnel,
    [switch]$InstallAutoStart,
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

function Get-TailscaleIPv4() {
    try {
        $ts = (tailscale ip -4 2>$null | Select-Object -First 1).Trim()
        if ($ts) { return $ts }
    } catch {}
    return ""
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

Write-Step "Cài dependencies cơ bản"
& $venvPy -m pip install --upgrade pip
& $venvPip install -r ".\requirements.txt"

Write-Step "Cấu hình GPU runtime"
& "$PSScriptRoot\setup_gpu_windows.ps1"

Write-Step "Tạo/Cập nhật .env cho máy HOST GPU"
$envFile = ".\.env"
if (!(Test-Path $envFile)) {
    Copy-Item ".\.env.example" $envFile
}

Ensure-EnvLine $envFile "HOST" $Host
Ensure-EnvLine $envFile "PORT" "$Port"
Ensure-EnvLine $envFile "PADDLE_USE_GPU" "true"
Ensure-EnvLine $envFile "INTERNAL_GPU_URL" "http://127.0.0.1:$Port"
Ensure-EnvLine $envFile "INTERNAL_GPU_TOKEN" ""
Ensure-EnvLine $envFile "REMOTE_WORKER_TOKEN" $WorkerToken
Ensure-EnvLine $envFile "REMOTE_POLL_INTERVAL_SECONDS" "1.5"
Ensure-EnvLine $envFile "REMOTE_REQUEST_TIMEOUT_SECONDS" "120"

Write-Step "Mở firewall inbound TCP $Port (nếu chưa có)"
$ruleName = "OCR Service $Port"
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port | Out-Null
    Write-Host "Đã tạo firewall rule: $ruleName" -ForegroundColor Green
} else {
    Write-Host "Firewall rule đã tồn tại: $ruleName" -ForegroundColor Yellow
}

Write-Step "Thông tin endpoint host"
$lanIPs = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "169.254.*" -and $_.IPAddress -ne "127.0.0.1" } |
    Select-Object -ExpandProperty IPAddress -Unique
$tsIP = Get-TailscaleIPv4

Write-Host "LAN IP(s): $($lanIPs -join ', ')" -ForegroundColor Green
if ($tsIP) {
    Write-Host "Tailscale IP: $tsIP" -ForegroundColor Green
    Write-Host "Client INTERNAL_GPU_URL đề xuất: http://$tsIP:$Port" -ForegroundColor Green
} elseif ($lanIPs) {
    Write-Host "Client INTERNAL_GPU_URL đề xuất: http://$($lanIPs[0]):$Port" -ForegroundColor Green
}
if ($WorkerToken) {
    Write-Host "Worker token: $WorkerToken" -ForegroundColor Yellow
} else {
    Write-Host "Worker token đang rỗng (REMOTE_WORKER_TOKEN='')." -ForegroundColor Yellow
}

if ($EnableTailscaleServe -or $EnableTailscaleFunnel) {
    Write-Step "Publish link qua Tailscale"
    $publishScript = Join-Path $PSScriptRoot "publish_tailscale_link.ps1"
    if ($EnableTailscaleFunnel) {
        & $publishScript -Port $Port -Public
    } else {
        & $publishScript -Port $Port
    }
}

if ($InstallAutoStart) {
    Write-Step "Cài auto-start khi boot"
    $autoScript = Join-Path $PSScriptRoot "install_host_autostart.ps1"
    & $autoScript
}

if ($SkipRun) {
    Write-Host "`nSetup xong. Chạy server bằng lệnh:" -ForegroundColor Cyan
    Write-Host "  .\venv\Scripts\python.exe -m uvicorn app.main:app --host $Host --port $Port" -ForegroundColor White
    exit 0
}

Write-Step "Chạy OCR service"
& $venvPy -m uvicorn app.main:app --host $Host --port $Port
