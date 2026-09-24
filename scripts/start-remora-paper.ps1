# Keyless paper training: real public BTC/ETH USD-M prices, simulated fills. No orders are sent.
#   -ModelDecisions : experimental v2 model decides long/cash (walk-forward gate FAILED)
param([string]$PythonPath = "python", [switch]$ModelDecisions)
$root = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Force (Join-Path $root "state") | Out-Null
if ($ModelDecisions) { $env:REMORA_MODEL_DECISIONS = "true" }
try {
    $p = Start-Process -FilePath $PythonPath -ArgumentList @("-u", "-m", "remora_bot", "run", "--mode", "paper") `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $root "state\remora-bot-paper.out.log") `
        -RedirectStandardError (Join-Path $root "state\remora-bot-paper.err.log")
    Write-Host "Remora paper egitimi basladi. PID=$($p.Id)"
    Write-Host "Durum: python -m remora_bot status --mode paper"
}
finally {
    Remove-Item Env:REMORA_MODEL_DECISIONS -ErrorAction SilentlyContinue
}
