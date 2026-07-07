# Thiet lap SSH deploy cho may host OCR (Windows OpenSSH).
# Chay PowerShell **Run as Administrator**:
#   powershell -ExecutionPolicy Bypass -File .\ocr-release-kit\Setup-SshDeploy.ps1
# Them public key tu may deploy:
#   powershell -ExecutionPolicy Bypass -File .\ocr-release-kit\Setup-SshDeploy.ps1 -PublicKey "ssh-ed25519 AAAA... deploy@ci"

param(
    [string]$PublicKey = "",
    [int]$Port = 22
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Require-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Can quyen Administrator. Mo PowerShell -> Run as administrator."
    }
}

Require-Admin

Write-Host "=== OCR SSH DEPLOY SETUP ===" -ForegroundColor Cyan

# 1) OpenSSH Server
$cap = Get-WindowsCapability -Online | Where-Object { $_.Name -eq "OpenSSH.Server~~~~0.0.1.0" }
if ($cap -and $cap.State -ne "Installed") {
    Write-Host "Cai OpenSSH Server..." -ForegroundColor Yellow
    Add-WindowsCapability -Online -Name "OpenSSH.Server~~~~0.0.1.0"
}

$svc = Get-Service sshd -ErrorAction SilentlyContinue
if (-not $svc) {
    throw "Khong tim thay service sshd."
}
if ($svc.Status -ne "Running") {
    Start-Service sshd
}
Set-Service sshd -StartupType Automatic
Write-Host "[OK] sshd: Running, Automatic" -ForegroundColor Green

# 2) Firewall inbound port 22
$ruleName = "OCR SSH Deploy (TCP 22)"
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if (-not $existing) {
    New-NetFirewallRule `
        -DisplayName $ruleName `
        -Direction Inbound `
        -Protocol TCP `
        -LocalPort $Port `
        -Action Allow `
        -Profile Any | Out-Null
    Write-Host "[OK] Firewall rule: $ruleName" -ForegroundColor Green
} else {
    Write-Host "[OK] Firewall rule da co" -ForegroundColor Green
}

# 3) authorized_keys cho user thuoc Administrators (Windows OpenSSH)
$adminKeys = "C:\ProgramData\ssh\administrators_authorized_keys"
if ($PublicKey.Trim()) {
    $keyLine = $PublicKey.Trim()
    if (-not (Test-Path $adminKeys)) {
        New-Item -ItemType File -Path $adminKeys -Force | Out-Null
    }
    $content = ""
    if ((Get-Item $adminKeys).Length -gt 0) {
        $content = Get-Content $adminKeys -Raw -Encoding UTF8
    }
    if ($content -notmatch [Regex]::Escape($keyLine)) {
        Add-Content -Path $adminKeys -Value $keyLine -Encoding UTF8
        Write-Host "[OK] Da them public key vao administrators_authorized_keys" -ForegroundColor Green
    } else {
        Write-Host "[OK] Public key da ton tai" -ForegroundColor Green
    }
    icacls $adminKeys /inheritance:r | Out-Null
    icacls $adminKeys /grant "SYSTEM:(F)" | Out-Null
    icacls $adminKeys /grant "BUILTIN\Administrators:(F)" | Out-Null
} else {
    if (-not (Test-Path $adminKeys)) {
        New-Item -ItemType File -Path $adminKeys -Force | Out-Null
        icacls $adminKeys /inheritance:r | Out-Null
        icacls $adminKeys /grant "SYSTEM:(F)" | Out-Null
        icacls $adminKeys /grant "BUILTIN\Administrators:(F)" | Out-Null
        Write-Host "[!] Da tao $adminKeys - hay them public key may deploy" -ForegroundColor Yellow
    } else {
        Write-Host "[OK] $adminKeys da ton tai" -ForegroundColor Green
    }
}

# 4) Thong tin ket noi
$user = $env:USERNAME
$hostName = $env:COMPUTERNAME
$tsIp = ""
try {
    $tsIp = (tailscale ip -4 2>$null | Select-Object -First 1).Trim()
} catch {}

$lanIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {
        $_.IPAddress -notlike "169.254.*" `
            -and $_.IPAddress -ne "127.0.0.1" `
            -and $_.IPAddress -notlike "100.*" `
            -and $_.PrefixOrigin -ne "WellKnown"
    } |
    Select-Object -ExpandProperty IPAddress -First 1

Write-Host ""
Write-Host "=== THONG TIN SSH ===" -ForegroundColor Cyan
Write-Host "User     : $user"
Write-Host "Hostname : $hostName"
if ($tsIp) {
    Write-Host "Tailscale: ssh ${user}@${tsIp}" -ForegroundColor Green
}
if ($lanIp) {
    Write-Host "LAN      : ssh ${user}@${lanIp}"
}
Write-Host ""
Write-Host "Test tu may deploy:" -ForegroundColor Yellow
if ($tsIp) {
    Write-Host "  ssh ${user}@${tsIp}"
}
Write-Host "  ssh ${user}@${hostName}"
Write-Host ""
Write-Host "Deploy OCR (sau khi SSH duoc):" -ForegroundColor Yellow
$deployExample = '  ssh user@host powershell -ExecutionPolicy Bypass -File C:/Projects/ocr-banca/ocr-release-kit/Deploy-Remote.ps1'
Write-Host $deployExample
