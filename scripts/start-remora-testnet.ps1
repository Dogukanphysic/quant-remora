# Binance USD-M Futures virtual-money environments only. No mainnet path exists.
#   -Environment demo    : keys from binance.com > Demo Trading > API Management (demo-fapi.binance.com)
#   -Environment testnet : keys from testnet.binancefuture.com
# Keys are read hidden, live only in this process and the detached bot, and are never written to disk.
#   -ModelDecisions      : experimental v2 model decides long/cash (walk-forward gate FAILED; virtual money only)
param([string]$PythonPath = "python",
      [ValidateSet("demo", "testnet")][string]$Environment = "demo",
      [switch]$ModelDecisions)
$root = Split-Path -Parent $PSScriptRoot
$confirm = "REMORA BTC ETH FUTURES TESTNET BASLAT"

function Read-Secret([string]$prompt) {
    $secure = Read-Host $prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

Write-Host "Once paper egitimi calisiyorsa durdurun: .\scripts\stop-remora-bot.ps1 -Mode paper"
Write-Host "Ortam: $Environment  (demo = binance.com Demo Trading anahtari, testnet = testnet.binancefuture.com anahtari)"
$env:REMORA_FUTURES_TEST_ENV = $Environment
if ($ModelDecisions) {
    $env:REMORA_MODEL_DECISIONS = "true"
    Write-Host "DENEYSEL MODEL KARARLARI ACIK: model walk-forward kapisini gecmedi; yalniz sanal para."
}
$env:REMORA_FUTURES_TESTNET_API_KEY = Read-Secret "Futures Testnet API key"
$env:REMORA_FUTURES_TESTNET_SECRET_KEY = Read-Secret "Futures Testnet secret key"
try {
    $answer = (Read-Host "Onay icin tam olarak yazin (Turkce karakter yok, BASLAT): $confirm").Trim()
    if ($answer -ne $confirm) { throw "Onay cumlesi eslesmedi (girilen: '$answer'); bot baslatilmadi." }
    $env:REMORA_TESTNET_CONFIRM = $confirm
    $p = Start-Process -FilePath $PythonPath -ArgumentList @("-m", "remora_bot", "run", "--mode", "testnet") `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $root "state\remora-bot-testnet.out.log") `
        -RedirectStandardError (Join-Path $root "state\remora-bot-testnet.err.log")
    Write-Host "Remora Testnet botu basladi. PID=$($p.Id)"
    Write-Host "Durum: python -m remora_bot status --mode testnet"
}
finally {
    Remove-Item Env:REMORA_FUTURES_TESTNET_API_KEY, Env:REMORA_FUTURES_TESTNET_SECRET_KEY, Env:REMORA_TESTNET_CONFIRM, Env:REMORA_FUTURES_TEST_ENV, Env:REMORA_MODEL_DECISIONS -ErrorAction SilentlyContinue
}
