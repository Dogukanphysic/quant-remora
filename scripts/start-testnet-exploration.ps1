[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$previousExplore = $env:BINANCE_TESTNET_EXPLORATION
$previousMode = $env:BINANCE_TESTNET_POLICY_MODE
$previousInterval = $env:BINANCE_TESTNET_DECISION_INTERVAL
$previousAuthority = $env:BINANCE_TESTNET_AUTO_MODEL
try {
    $env:BINANCE_TESTNET_EXPLORATION = 'true'
    $env:BINANCE_TESTNET_POLICY_MODE = 'hourly'
    $env:BINANCE_TESTNET_DECISION_INTERVAL = '15m'
    $env:BINANCE_TESTNET_AUTO_MODEL = 'true'
    Write-Host 'Yalniz BTCUSDT TESTNET: 15 sanal USDT, saatte bir kesif girisi, 15-30 dakika hedef tutma.'
    Write-Host 'Karsiz tahminde de kesif islemi acabilir. Mainnet ADA surecine dokunulmaz.'
    & (Join-Path $PSScriptRoot 'start-binance-testnet-agent.ps1') -Restart -EntryQuoteUsdt 15
}
finally {
    $env:BINANCE_TESTNET_EXPLORATION = $previousExplore
    $env:BINANCE_TESTNET_POLICY_MODE = $previousMode
    $env:BINANCE_TESTNET_DECISION_INTERVAL = $previousInterval
    $env:BINANCE_TESTNET_AUTO_MODEL = $previousAuthority
}
