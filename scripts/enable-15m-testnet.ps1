[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$previousInterval = $env:BINANCE_TESTNET_DECISION_INTERVAL
try {
    $env:BINANCE_TESTNET_DECISION_INTERVAL = '15m'
    Write-Host 'Karar ve egitim 15m olacak. Acik Testnet pozisyonu ve islem defteri korunur.'
    & (Join-Path $PSScriptRoot 'enable-hourly-model-authority.ps1')
}
finally {
    $env:BINANCE_TESTNET_DECISION_INTERVAL = $previousInterval
}
