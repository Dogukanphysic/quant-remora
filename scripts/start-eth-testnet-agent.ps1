[CmdletBinding()]
param(
    [string]$PythonPath = 'python',
    [ValidateSet('Run','Status','Stop')][string]$Action = 'Run'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$apiSecure = $null; $secretSecure = $null
$apiPointer = [IntPtr]::Zero; $secretPointer = [IntPtr]::Zero
$previousPolicy = $env:BINANCE_TESTNET_POLICY_MODE
$previousInterval = $env:BINANCE_TESTNET_DECISION_INTERVAL
$env:BINANCE_TESTNET_POLICY_MODE = 'hourly'
$env:BINANCE_TESTNET_DECISION_INTERVAL = '15m'

if ($Action -eq 'Status') {
    & $PythonPath (Join-Path $projectRoot 'eth_testnet_worker.py') status
    exit $LASTEXITCODE
}
if ($Action -eq 'Stop') {
    & $PythonPath (Join-Path $projectRoot 'eth_testnet_worker.py') stop
    exit $LASTEXITCODE
}

try {
    Write-Host 'BINANCE SPOT TESTNET ETHUSDT: sanal Testnet emirleri; gercek para kullanilmaz.'
    Write-Host '15m karar, 24 saat momentum ve islem basina 15 sanal USDT kullanilir.'
    $answer = Read-Host 'Devam icin BINANCE ETH TESTNET AGENTI BASLAT yazin'
    if ($answer -cne 'BINANCE ETH TESTNET AGENTI BASLAT') { throw 'Onay eslesmedi.' }
    $apiSecure = Read-Host 'Binance Spot Testnet API key' -AsSecureString
    $secretSecure = Read-Host 'Binance Spot Testnet secret key' -AsSecureString
    if ($apiSecure.Length -eq 0 -or $secretSecure.Length -eq 0) { throw 'Anahtar bos olamaz.' }
    $apiPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiSecure)
    $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secretSecure)
    $env:BINANCE_TESTNET_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiPointer)
    $env:BINANCE_TESTNET_SECRET_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiPointer); $apiPointer = [IntPtr]::Zero
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer); $secretPointer = [IntPtr]::Zero
    $env:BINANCE_ORDER_EXECUTION_ENABLED = 'testnet'
    $env:BINANCE_TESTNET_WORKER_ENABLED = 'true'
    & $PythonPath (Join-Path $projectRoot 'eth_testnet_worker.py') start
    if ($LASTEXITCODE -ne 0) { throw 'ETH Testnet worker baslatilamadi.' }
}
finally {
    Remove-Item Env:BINANCE_TESTNET_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:BINANCE_TESTNET_SECRET_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:BINANCE_ORDER_EXECUTION_ENABLED -ErrorAction SilentlyContinue
    Remove-Item Env:BINANCE_TESTNET_WORKER_ENABLED -ErrorAction SilentlyContinue
    $env:BINANCE_TESTNET_POLICY_MODE = $previousPolicy
    $env:BINANCE_TESTNET_DECISION_INTERVAL = $previousInterval
    if ($apiPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiPointer) }
    if ($secretPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer) }
    if ($null -ne $apiSecure) { $apiSecure.Dispose() }
    if ($null -ne $secretSecure) { $secretSecure.Dispose() }
}
