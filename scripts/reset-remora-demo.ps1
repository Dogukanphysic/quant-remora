# One command: close positions the bot does not own on the Demo/Testnet account (reduce-only market
# orders, virtual money), verify the account is clean, clear the halt, then start the exploration bot.
# Keys are asked once, kept only in this process and the detached bot, never written to disk.
param([string]$PythonPath = "python", [ValidateSet("demo", "testnet")][string]$Environment = "demo")
$root = Split-Path -Parent $PSScriptRoot
$confirm = "DEMO HESAPTAKI YABANCI POZISYONLARI KAPAT"

function Read-Secret([string]$prompt) {
    $secure = Read-Host $prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "stop-remora-bot.ps1") -Mode testnet | Out-Null
Start-Sleep -Seconds 2
$env:REMORA_FUTURES_TESTNET_API_KEY = Read-Secret "API key"
$env:REMORA_FUTURES_TESTNET_SECRET_KEY = Read-Secret "Secret key"
$env:REMORA_FUTURES_TEST_ENV = $Environment
try {
    $answer = (Read-Host "Onay icin tam olarak yazin: $confirm").Trim()
    if ($answer -ne $confirm) { throw "Onay cumlesi eslesmedi; hicbir sey yapilmadi." }
    $env:REMORA_RESET_CONFIRM = $confirm
    Push-Location $root
    & $PythonPath -m remora_bot reset-demo
    $ok = ($LASTEXITCODE -eq 0)
    Pop-Location
    Remove-Item Env:REMORA_RESET_CONFIRM -ErrorAction SilentlyContinue
    if (-not $ok) { throw "Temizlik tamamlanmadi; bot baslatilmadi." }

    $env:REMORA_TESTNET_CONFIRM = "REMORA BTC ETH FUTURES TESTNET BASLAT"
    $env:REMORA_MODEL_DECISIONS = "true"
    $env:REMORA_INTERVAL = "1h"
    $env:REMORA_EXPLORE = "true"
    $p = Start-Process -FilePath $PythonPath -ArgumentList @("-u", "-m", "remora_bot", "run", "--mode", "testnet") `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $root "state\remora-bot-testnet.out.log") `
        -RedirectStandardError (Join-Path $root "state\remora-bot-testnet.err.log")
    Write-Host "Hesap temizlendi, bot kesif moduyla basladi. PID=$($p.Id)"
}
finally {
    Remove-Item Env:REMORA_FUTURES_TESTNET_API_KEY, Env:REMORA_FUTURES_TESTNET_SECRET_KEY, Env:REMORA_FUTURES_TEST_ENV, `
        Env:REMORA_RESET_CONFIRM, Env:REMORA_TESTNET_CONFIRM, Env:REMORA_MODEL_DECISIONS, Env:REMORA_INTERVAL, `
        Env:REMORA_EXPLORE -ErrorAction SilentlyContinue
}
