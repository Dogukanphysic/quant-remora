# Keyless paper training: real public BTC/ETH USD-M prices, simulated fills. No orders are sent.
param([string]$PythonPath = "python")
$root = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Force (Join-Path $root "state") | Out-Null
$p = Start-Process -FilePath $PythonPath -ArgumentList @("-m", "remora_bot", "run", "--mode", "paper") `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $root "state\remora-bot-paper.out.log") `
    -RedirectStandardError (Join-Path $root "state\remora-bot-paper.err.log")
Write-Host "Remora paper egitimi basladi. PID=$($p.Id)"
Write-Host "Durum: python -m remora_bot status --mode paper"
