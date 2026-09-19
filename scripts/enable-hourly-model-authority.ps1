[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$previousMode = $env:BINANCE_TESTNET_POLICY_MODE
$previousAuthority = $env:BINANCE_TESTNET_AUTO_MODEL
try {
    $env:BINANCE_TESTNET_POLICY_MODE = 'hourly'
    $env:BINANCE_TESTNET_AUTO_MODEL = 'true'
    $decisionFrame = if ($env:BINANCE_TESTNET_DECISION_INTERVAL) { $env:BINANCE_TESTNET_DECISION_INTERVAL } else { '1h' }
    Write-Host "Testnet worker $decisionFrame karar araligiyla yeniden baslatilacak; takipli pozisyon ve defter korunur."
    Write-Host 'Dogrulama kapisini gecen model karar verebilir; diger durumda momentum kurali surer.'
    & (Join-Path $PSScriptRoot 'start-binance-testnet-agent.ps1') -Restart -EntryQuoteUsdt 15
}
finally {
    $env:BINANCE_TESTNET_POLICY_MODE = $previousMode
    $env:BINANCE_TESTNET_AUTO_MODEL = $previousAuthority
}
