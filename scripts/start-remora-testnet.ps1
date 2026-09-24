# Binance USD-M Futures TESTNET only (https://testnet.binancefuture.com). No mainnet path exists.
# Keys are read hidden, live only in this process and the detached bot, and are never written to disk.
param([string]$PythonPath = "python")
$root = Split-Path -Parent $PSScriptRoot
$confirm = "REMORA BTC ETH FUTURES TESTNET BASLAT"

function Read-Secret([string]$prompt) {
    $secure = Read-Host $prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

Write-Host "Once paper egitimi calisiyorsa durdurun: .\scripts\stop-remora-bot.ps1 -Mode paper"
Write-Host "Anahtarlar testnet.binancefuture.com uzerinden alinmis Futures TESTNET anahtarlari olmalidir."
$env:REMORA_FUTURES_TESTNET_API_KEY = Read-Secret "Futures Testnet API key"
$env:REMORA_FUTURES_TESTNET_SECRET_KEY = Read-Secret "Futures Testnet secret key"
try {
    $answer = Read-Host "Onay icin tam olarak yazin: $confirm"
    if ($answer -ne $confirm) { throw "Onay cumlesi eslesmedi; bot baslatilmadi." }
    $env:REMORA_TESTNET_CONFIRM = $confirm
    $p = Start-Process -FilePath $PythonPath -ArgumentList @("-m", "remora_bot", "run", "--mode", "testnet") `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $root "state\remora-bot-testnet.out.log") `
        -RedirectStandardError (Join-Path $root "state\remora-bot-testnet.err.log")
    Write-Host "Remora Testnet botu basladi. PID=$($p.Id)"
    Write-Host "Durum: python -m remora_bot status --mode testnet"
}
finally {
    Remove-Item Env:REMORA_FUTURES_TESTNET_API_KEY, Env:REMORA_FUTURES_TESTNET_SECRET_KEY, Env:REMORA_TESTNET_CONFIRM -ErrorAction SilentlyContinue
}
