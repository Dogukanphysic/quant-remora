# Read-only: lists positions and open orders in the bot's 10 coins. Sends no orders, changes nothing.
param([string]$PythonPath = "python", [ValidateSet("demo", "testnet")][string]$Environment = "demo")
$root = Split-Path -Parent $PSScriptRoot

function Read-Secret([string]$prompt) {
    $secure = Read-Host $prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

$env:REMORA_FUTURES_TESTNET_API_KEY = Read-Secret "API key"
$env:REMORA_FUTURES_TESTNET_SECRET_KEY = Read-Secret "Secret key"
$env:REMORA_FUTURES_TEST_ENV = $Environment
try {
    Push-Location $root
    & $PythonPath -m remora_bot positions
}
finally {
    Pop-Location
    Remove-Item Env:REMORA_FUTURES_TESTNET_API_KEY, Env:REMORA_FUTURES_TESTNET_SECRET_KEY, Env:REMORA_FUTURES_TEST_ENV -ErrorAction SilentlyContinue
}
