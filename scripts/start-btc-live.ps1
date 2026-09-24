[CmdletBinding()]
param(
    [string]$PythonPath = 'python',
    [ValidateSet('Run','Diagnose','OrderCheck','Status')][string]$Action = 'Run'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$apiSecure = $null
$secretSecure = $null
$apiPointer = [IntPtr]::Zero
$secretPointer = [IntPtr]::Zero
$previousEncoding = $env:PYTHONIOENCODING

if ($Action -eq 'Status') {
    & $PythonPath (Join-Path $projectRoot 'btc_live.py') status --interval 15m
    exit $LASTEXITCODE
}

try {
    if ($Action -eq 'Run') {
        Write-Host 'BINANCE MAINNET BTCUSDT: Bu komut gercek emir gonderebilir.'
        Write-Host '15 dakikalik kapali mumlar, gevsek iki-kanitli Bollinger yorumu ve tum tahsisli BTC/USDT kullanilir.'
        Write-Host 'Stratejinin karli oldugu kanitlanmamistir; stop ve hedef kurallari emirlerden once gelir.'
        $answer = Read-Host 'Devam icin TUM TAHSISLI USDT ILE BTC GERCEK ISLEM BASLAT yazin'
        if ($answer -cne 'TUM TAHSISLI USDT ILE BTC GERCEK ISLEM BASLAT') {
            throw 'Onay eslesmedi; hicbir islem baslatilmadi.'
        }
    }
    else {
        Write-Host 'Bu kontrol gercek emir gondermez.'
    }

    $apiSecure = Read-Host 'Binance MAINNET API key' -AsSecureString
    $secretSecure = Read-Host 'Binance MAINNET secret key' -AsSecureString
    if ($apiSecure.Length -eq 0 -or $secretSecure.Length -eq 0) { throw 'Anahtar bos olamaz.' }
    $apiPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiSecure)
    $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secretSecure)
    $env:BTC_MAINNET_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiPointer)
    $env:BTC_MAINNET_SECRET_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiPointer); $apiPointer = [IntPtr]::Zero
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer); $secretPointer = [IntPtr]::Zero
    $env:BTC_LIVE_AUTHORIZATION = 'TUM TAHSISLI USDT ILE BTC GERCEK ISLEM BASLAT'
    $env:PYTHONIOENCODING = 'utf-8'

    switch ($Action) {
        'Diagnose' { & $PythonPath (Join-Path $projectRoot 'btc_live.py') diagnose --live --interval 15m }
        'OrderCheck' { & $PythonPath (Join-Path $projectRoot 'btc_live.py') order-check --live --interval 15m }
        'Run' { & $PythonPath (Join-Path $projectRoot 'btc_live.py') run --live --interval 15m --bollinger-touch --use-all-allocated-funds }
    }
    if ($LASTEXITCODE -ne 0) { throw 'BTC worker hata ile sonlandi; btc-live durum kaydini kontrol edin.' }
}
finally {
    Remove-Item Env:BTC_MAINNET_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:BTC_MAINNET_SECRET_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:BTC_LIVE_AUTHORIZATION -ErrorAction SilentlyContinue
    $env:PYTHONIOENCODING = $previousEncoding
    if ($apiPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiPointer) }
    if ($secretPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer) }
    if ($null -ne $apiSecure) { $apiSecure.Dispose() }
    if ($null -ne $secretSecure) { $secretSecure.Dispose() }
}
