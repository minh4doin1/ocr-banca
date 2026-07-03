# Publish OCR host to one Tailscale URL.
#
# Modes:
# - Internal tailnet URL (recommended): tailscale serve --bg 8100
# - Public URL (optional): tailscale funnel --bg 8100
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\publish_tailscale_link.ps1
#   powershell -ExecutionPolicy Bypass -File .\scripts\publish_tailscale_link.ps1 -Public
#   powershell -ExecutionPolicy Bypass -File .\scripts\publish_tailscale_link.ps1 -Port 8100

param(
    [int]$Port = 8100,
    [switch]$Public
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command tailscale -ErrorAction SilentlyContinue)) {
    throw "Không tìm thấy tailscale CLI. Cài Tailscale trước."
}

Write-Host "=== Tailscale status ===" -ForegroundColor Cyan
tailscale status | Out-Host

if ($Public) {
    Write-Host "`n=== Enable public Funnel ===" -ForegroundColor Cyan
    tailscale funnel --bg $Port | Out-Host
    Write-Host "`nPublic URL (Funnel) đã bật. Chạy 'tailscale funnel status' để xem link." -ForegroundColor Green
} else {
    Write-Host "`n=== Enable internal Serve ===" -ForegroundColor Cyan
    tailscale serve --bg $Port | Out-Host
    Write-Host "`nTailnet URL đã bật. Chạy 'tailscale serve status' để xem link." -ForegroundColor Green
}
