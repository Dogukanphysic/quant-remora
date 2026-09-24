# Read-only key check: reports which virtual-money Binance environment accepts the key.
# Sends only account READ requests to test hosts; never contacts mainnet, never places orders.
param([string]$PythonPath = "python")
$root = Split-Path -Parent $PSScriptRoot

function Read-Secret([string]$prompt) {
    $secure = Read-Host $prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

$env:REMORA_FUTURES_TESTNET_API_KEY = Read-Secret "API key"
$env:REMORA_FUTURES_TESTNET_SECRET_KEY = Read-Secret "Secret key"
try {
    Push-Location $root
    & $PythonPath -m remora_bot keycheck
}
finally {
    Pop-Location
    Remove-Item Env:REMORA_FUTURES_TESTNET_API_KEY, Env:REMORA_FUTURES_TESTNET_SECRET_KEY -ErrorAction SilentlyContinue
}
