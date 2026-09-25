# Keyless paper training: real public USD-M prices, simulated fills. No orders are sent.
#   -ModelDecisions : experimental v2 model decides long/cash (walk-forward gate FAILED)
#   -Interval 1h    : hourly decisions (requires -ModelDecisions); default 4h
#   -Explore        : 10-coin exploration, learns from every trade (needs -ModelDecisions -Interval 1h)
param([string]$PythonPath = "python", [switch]$ModelDecisions,
      [ValidateSet("4h", "1h")][string]$Interval = "4h", [switch]$Explore)
if ($Interval -eq "1h" -and -not $ModelDecisions) { throw "-Interval 1h yalniz -ModelDecisions ile calisir; bot baslatilmadi." }
if ($Explore -and -not ($ModelDecisions -and $Interval -eq "1h")) { throw "-Explore icin -ModelDecisions -Interval 1h gerekir; bot baslatilmadi." }
$root = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Force (Join-Path $root "state") | Out-Null
if ($ModelDecisions) { $env:REMORA_MODEL_DECISIONS = "true" }
if ($Explore) { $env:REMORA_EXPLORE = "true" }
$env:REMORA_INTERVAL = $Interval
try {
    $p = Start-Process -FilePath $PythonPath -ArgumentList @("-u", "-m", "remora_bot", "run", "--mode", "paper") `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $root "state\remora-bot-paper.out.log") `
        -RedirectStandardError (Join-Path $root "state\remora-bot-paper.err.log")
    Write-Host "Remora paper egitimi basladi ($Interval). PID=$($p.Id)"
    Write-Host "Durum: python -m remora_bot status --mode paper"
}
finally {
    Remove-Item Env:REMORA_MODEL_DECISIONS, Env:REMORA_INTERVAL, Env:REMORA_EXPLORE -ErrorAction SilentlyContinue
}
