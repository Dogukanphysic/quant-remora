[CmdletBinding()]
param(
    [string]$PythonPath = 'python',
    [ValidateSet('Run','Configure','Diagnose','OrderCheck','Status')]
    [string]$Action = 'Status',
    [ValidateSet('testnet','demo')][string]$Environment = 'testnet',
    [decimal]$MarginUsdt = 1000,
    [switch]$UseAllAvailableBalance,
    [int]$PollSeconds = 30
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$script = Join-Path $projectRoot 'btc_futures_testnet.py'
$apiSecure = $null; $secretSecure = $null
$apiPointer = [IntPtr]::Zero; $secretPointer = [IntPtr]::Zero
$previousEncoding = $env:PYTHONIOENCODING
if ($Action -eq 'Status') { & $PythonPath $script status; exit $LASTEXITCODE }
if ($MarginUsdt -le 0) { throw 'MarginUsdt sifirdan buyuk olmali.' }
try {
    Write-Host "BINANCE BTCUSDT USD-M FUTURES $($Environment.ToUpper())"
    Write-Host 'Sanal fon, isolated 2x, long-only, kapanmis 15m mum ve sonuc ogrenmesi.'
    if ($Action -in @('Configure','Run')) {
        $answer = Read-Host 'Devam icin BTC FUTURES TESTNET AJANINI BASLAT yazin'
        if ($answer -cne 'BTC FUTURES TESTNET AJANINI BASLAT') { throw 'Onay eslesmedi.' }
    } else { Write-Host 'Bu kontrol emir gondermez.' }
    $apiSecure = Read-Host 'Binance Futures test API key' -AsSecureString
    $secretSecure = Read-Host 'Binance Futures test secret key' -AsSecureString
    if ($apiSecure.Length -eq 0 -or $secretSecure.Length -eq 0) { throw 'Anahtar bos olamaz.' }
    $apiPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiSecure)
    $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secretSecure)
    $env:BTC_FUTURES_TESTNET_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiPointer)
    $env:BTC_FUTURES_TESTNET_SECRET_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiPointer); $apiPointer = [IntPtr]::Zero
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer); $secretPointer = [IntPtr]::Zero
    $env:BTC_FUTURES_TESTNET_AUTHORIZATION = 'BTC FUTURES TESTNET AJANINI BASLAT'
    $env:PYTHONIOENCODING = 'utf-8'
    $argsList = @('--enabled','--environment',$Environment,'--margin-usdt',$MarginUsdt)
    if ($UseAllAvailableBalance) { $argsList += '--use-all-available-balance' }
    switch ($Action) {
        'Diagnose' { & $PythonPath $script diagnose @argsList }
        'OrderCheck' { & $PythonPath $script order-check @argsList }
        'Configure' { & $PythonPath $script configure @argsList }
        'Run' {
            $diagnoseLines = @(& $PythonPath $script diagnose @argsList)
            $diagnoseExit = $LASTEXITCODE
            $diagnoseText = $diagnoseLines -join [Environment]::NewLine
            Write-Host $diagnoseText
            if ($diagnoseExit -ne 0) { throw 'Futures testnet diagnose komutu basarisiz.' }
            try { $diagnose = $diagnoseText | ConvertFrom-Json }
            catch { throw 'Futures testnet diagnose JSON ciktisi okunamadi.' }
            if (-not $diagnose.ok) {
                $failed = @()
                foreach ($property in $diagnose.checks.PSObject.Properties) {
                    if (-not $property.Value.ok) {
                        $reason = if ($property.Value.reason) { $property.Value.reason } else { 'basarisiz' }
                        $failed += ($property.Name + ': ' + $reason)
                    }
                }
                throw ('Futures testnet on kontrol basarisiz -> ' + ($failed -join ' | '))
            }
            & $PythonPath $script order-check @argsList
            if ($LASTEXITCODE -ne 0) { throw 'Futures testnet order/test basarisiz.' }
            & $PythonPath $script run @argsList --poll-seconds $PollSeconds
        }
    }
    if ($LASTEXITCODE -ne 0) { throw 'BTC Futures testnet worker hata ile sonlandi.' }
}
finally {
    Remove-Item Env:BTC_FUTURES_TESTNET_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:BTC_FUTURES_TESTNET_SECRET_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:BTC_FUTURES_TESTNET_AUTHORIZATION -ErrorAction SilentlyContinue
    $env:PYTHONIOENCODING = $previousEncoding
    if ($apiPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiPointer) }
    if ($secretPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer) }
    if ($null -ne $apiSecure) { $apiSecure.Dispose() }
    if ($null -ne $secretSecure) { $secretSecure.Dispose() }
}
