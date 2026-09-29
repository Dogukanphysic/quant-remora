# Stops the bot process. Open positions keep their exchange-side protective stop; nothing is sold.
param([ValidateSet("paper", "testnet", "carry")][string]$Mode = "paper")
$root = Split-Path -Parent $PSScriptRoot
$name = if ($Mode -eq "carry") { "carry-testnet" } else { $Mode }
$statusFile = Join-Path $root "state\remora-bot-$name-status.json"
if (-not (Test-Path $statusFile)) { Write-Host "Durum dosyasi yok; calisan $Mode botu bulunamadi."; exit 0 }
$botPid = (Get-Content $statusFile -Raw | ConvertFrom-Json).pid
$proc = Get-Process -Id $botPid -ErrorAction SilentlyContinue
if ($proc -and $proc.ProcessName -like "python*") {
    Stop-Process -Id $botPid -Confirm:$false
    Write-Host "Remora $Mode botu durduruldu (PID=$botPid). Pozisyonlar satilmadi."
} else {
    Write-Host "PID=$botPid calismiyor."
}
