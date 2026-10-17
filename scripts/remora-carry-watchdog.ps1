# Restarts the carry bot if its process died (virtual money, Futures Demo). Run by the scheduled task that
# install-remora-carry-watchdog.ps1 creates. It never repairs or clears a halt: a halted ledger needs a person.
# Skips while state\carry-watchdog.pause exists (stop-remora-bot.ps1 -Mode carry creates it).
param([string]$PythonPath = "python")
$root = Split-Path -Parent $PSScriptRoot
$state = Join-Path $root "state"
$log = Join-Path $state "remora-carry-watchdog.log"
$keys = Join-Path $state "carry-keys.dpapi"
function Log([string]$msg) { Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format "dd.MM HH:mm:ss"), $msg) }

if (Test-Path (Join-Path $state "carry-watchdog.pause")) { exit 0 }
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "remora_bot\s+run-carry" }
if ($running) { exit 0 }
if (-not (Test-Path $keys)) { Log "bot not running and no saved keys; run install-remora-carry-watchdog.ps1"; exit 1 }

$saved = Get-Content $keys -Raw | ConvertFrom-Json
function Plain([string]$enc) {
    $secure = ConvertTo-SecureString $enc                       # DPAPI: only this Windows user on this PC
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}
$env:REMORA_UNIVERSE = $saved.universe
$env:REMORA_CARRY_ADAPTIVE = "true"
$env:REMORA_CARRY_GROSS_USDT = "$($saved.gross)"
Push-Location $root
$status = & $PythonPath -m remora_bot carry-status | ConvertFrom-Json
Pop-Location
if ($status.bot.halted) { Log "bot not running, ledger HALTED ($($status.bot.halted)); not restarting"; exit 1 }

try {
    $env:REMORA_FUTURES_TESTNET_API_KEY = Plain $saved.key
    $env:REMORA_FUTURES_TESTNET_SECRET_KEY = Plain $saved.secret
    $env:REMORA_FUTURES_TEST_ENV = $saved.environment
    $env:REMORA_CARRY_CONFIRM = "REMORA CARRY DEMO BASLAT"
    $p = Start-Process -FilePath $PythonPath -ArgumentList @("-u", "-m", "remora_bot", "run-carry") `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $state "remora-bot-carry-testnet.out.log") `
        -RedirectStandardError (Join-Path $state "remora-bot-carry-testnet.err.log")
    Log "bot was not running; restarted PID=$($p.Id)"
}
finally {
    Remove-Item Env:REMORA_FUTURES_TESTNET_API_KEY, Env:REMORA_FUTURES_TESTNET_SECRET_KEY, Env:REMORA_FUTURES_TEST_ENV, `
        Env:REMORA_CARRY_CONFIRM -ErrorAction SilentlyContinue
}
