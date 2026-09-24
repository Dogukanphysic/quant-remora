[CmdletBinding()]
param(
    [string]$PythonPath = 'python',
    [ValidateSet('Run','Configure','Diagnose','OrderCheck','Status','NetworkCheck','LearningStatus','LearningSync','LearningWatch')]
    [string]$Action = 'Status',
    [decimal]$MarginUsdt = 50,
    [switch]$UseAllAvailableBalance,
    [ValidateSet(4,10)]
    [int]$Leverage = 4,
    [switch]$ModelDecisions,
    [ValidateSet('Standard','Moderate','Aggressive')]
    [string]$RiskProfile = 'Moderate',
    [ValidateSet('Validated','Exploratory')]
    [string]$EntryMode = 'Validated',
    [ValidateSet('Trend','Responsive')]
    [string]$SignalProfile = 'Trend',
    [int]$PollSeconds = 30
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$apiSecure = $null
$secretSecure = $null
$apiPointer = [IntPtr]::Zero
$secretPointer = [IntPtr]::Zero
$previousEncoding = $env:PYTHONIOENCODING
$previousLeverage = $env:BTC_FUTURES_LEVERAGE
$previousModelDecisions = $env:BTC_FUTURES_MODEL_DECISIONS
$confirmation = '{0}X ISOLATED BTC FUTURES MAINNET AJANINI BASLAT' -f $Leverage

if ($Action -in @('LearningStatus','LearningSync','LearningWatch')) {
    $learningAction = @{
        LearningStatus = 'status'; LearningSync = 'sync'; LearningWatch = 'watch'
    }[$Action]
    $learningScript = Join-Path $projectRoot 'btc_futures_learning.py'
    $learningSource = Join-Path $projectRoot 'state\btc-futures-live.sqlite3'
    $learningDb = Join-Path $projectRoot 'state\btc-futures-mainnet-learning.sqlite3'
    Write-Host 'Yerel Futures ogrenme adayi; API anahtari kullanmaz, emir gondermez.'
    & $PythonPath $learningScript $learningAction --source $learningSource --db $learningDb --poll-seconds ([Math]::Min(60, [Math]::Max(5, $PollSeconds)))
    exit $LASTEXITCODE
}

if ($Action -eq 'Status') {
    & $PythonPath (Join-Path $projectRoot 'btc_futures_live.py') status
    exit $LASTEXITCODE
}
if ($Action -eq 'NetworkCheck') {
    & $PythonPath (Join-Path $projectRoot 'btc_futures_live.py') network-check
    exit $LASTEXITCODE
}
if ($MarginUsdt -le 0) { throw 'MarginUsdt sifirdan buyuk olmali.' }

try {
    Write-Host 'BINANCE MAINNET BTCUSDT USD-M FUTURES'
    if ($UseAllAvailableBalance) {
        Write-Host ('Isolated {0}x, long-only, kullanilabilir Futures USDT bakiyesi (emir tamponu dahil)' -f $Leverage)
    }
    else {
        Write-Host ('Isolated {0}x, long-only, ayrilan margin: {1} USDT' -f $Leverage, $MarginUsdt)
    }
    Write-Host ('Risk profili: {0} (Standard=2/4 ATR, Moderate=2.5/5 ATR, Aggressive=3/6 ATR)' -f $RiskProfile)
    Write-Host ('Giris modu: {0} (Exploratory, OOS karlilik kaniti olmadan sinyal acabilir)' -f $EntryMode)
    Write-Host ('Sinyal profili: {0} (Responsive: alt bolge + toparlanma + MACD iyilesmesi)' -f $SignalProfile)
    if ($ModelDecisions) {
        Write-Host 'Model karari: DENEYSEL. Uyumlu model girisi kabul/erteleyebilir; olumsuz puanlarda deterministik 1/3 kesif payi vardir.'
        Write-Host 'Model uyumsuz veya kullanilamazsa temel strateji devam eder. Dogrulanmis net kar veya karlilik kaniti yoktur.'
    }
    if ($Action -in @('Configure','Run')) {
        Write-Host 'Bu islem Futures hesap ayarini/emirlerini degistirebilir ve gercek kayba yol acabilir.'
        $answer = Read-Host ('Devam icin {0} yazin' -f $confirmation)
        if ($answer -cne $confirmation) {
            throw 'Onay eslesmedi; islem baslatilmadi.'
        }
    }
    else {
        Write-Host 'Bu kontrol gercek emir gondermez.'
    }

    $apiSecure = Read-Host 'Binance MAINNET Futures API key' -AsSecureString
    $secretSecure = Read-Host 'Binance MAINNET Futures secret key' -AsSecureString
    if ($apiSecure.Length -eq 0 -or $secretSecure.Length -eq 0) { throw 'Anahtar bos olamaz.' }
    $apiPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiSecure)
    $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secretSecure)
    $env:BTC_FUTURES_MAINNET_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiPointer)
    $env:BTC_FUTURES_MAINNET_SECRET_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiPointer); $apiPointer = [IntPtr]::Zero
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer); $secretPointer = [IntPtr]::Zero
    $env:BTC_FUTURES_LIVE_AUTHORIZATION = $confirmation
    $env:BTC_FUTURES_LEVERAGE = [string]$Leverage
    $env:BTC_FUTURES_MODEL_DECISIONS = if ($ModelDecisions) { '1' } else { '0' }
    $env:BTC_FUTURES_RISK_PROFILE = $RiskProfile.ToLowerInvariant()
    $env:BTC_FUTURES_ENTRY_MODE = $EntryMode.ToLowerInvariant()
    $env:BTC_FUTURES_SIGNAL_PROFILE = $SignalProfile.ToLowerInvariant()
    $env:PYTHONIOENCODING = 'utf-8'

    $script = Join-Path $projectRoot 'btc_futures_live.py'
    $allocationArgs = @('--margin-usdt', $MarginUsdt)
    if ($UseAllAvailableBalance) { $allocationArgs += '--use-all-available-balance' }
    $operationStartedSeconds = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    switch ($Action) {
        'Diagnose' { & $PythonPath $script diagnose --live @allocationArgs }
        'OrderCheck' { & $PythonPath $script order-check --live @allocationArgs }
        'Configure' {
            $savedErrorAction = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try {
                $configureLines = @(& $PythonPath $script configure --live @allocationArgs 2>&1)
                $configureExit = $LASTEXITCODE
            }
            finally {
                $ErrorActionPreference = $savedErrorAction
            }
            $configureText = ($configureLines | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
            Write-Host $configureText
            if ($configureExit -ne 0) {
                throw ('Futures yapilandirma basarisiz -> ' + $configureText)
            }
        }
        'Run' {
            $localStatusText = (& $PythonPath $script status) -join [Environment]::NewLine
            try { $localStatus = $localStatusText | ConvertFrom-Json }
            catch { throw 'Yerel Futures durum JSON ciktisi okunamadi.' }
            $isConfiguredRestart = $null -ne $localStatus.phase
            if ($isConfiguredRestart) {
                Write-Host "`n[restart] Mevcut Futures defteri uzlastiriliyor; pending giris/korumalar kontrol edilecek."
                & $PythonPath $script run --live @allocationArgs --poll-seconds $PollSeconds
                break
            }
            Write-Host "`n[1/3] Futures salt-okunur on kontroller"
            $diagnoseLines = @(& $PythonPath $script diagnose --live @allocationArgs)
            $diagnoseExit = $LASTEXITCODE
            $diagnoseText = $diagnoseLines -join [Environment]::NewLine
            Write-Host $diagnoseText
            if ($diagnoseExit -ne 0) { throw ('Futures diagnose komutu hata kodu verdi: ' + $diagnoseExit) }
            try { $diagnose = $diagnoseText | ConvertFrom-Json }
            catch { throw 'Futures diagnose JSON ciktisi okunamadi.' }
            if (-not $diagnose.ok) {
                $failed = @()
                foreach ($property in $diagnose.checks.PSObject.Properties) {
                    if (-not $property.Value.ok) {
                        $reason = if ($property.Value.reason) { $property.Value.reason } else { 'basarisiz' }
                        $failed += ($property.Name + ': ' + $reason)
                    }
                }
                throw ('Futures on kontrol basarisiz -> ' + ($failed -join ' | '))
            }
            Write-Host "`n[2/3] Futures /order/test (gercek emir gondermez)"
            & $PythonPath $script order-check --live @allocationArgs
            if ($LASTEXITCODE -ne 0) { throw 'Futures order/test basarisiz; API Futures yetkisini kontrol edin.' }
            Write-Host ("`n[3/3] Isolated {0}x worker" -f $Leverage)
            & $PythonPath $script run --live @allocationArgs --poll-seconds $PollSeconds
        }
    }
    if ($LASTEXITCODE -ne 0) {
        $workerExit = $LASTEXITCODE
        try {
            $failureStatus = ((& $PythonPath $script status) -join [Environment]::NewLine) | ConvertFrom-Json
            $detailProperty = $failureStatus.PSObject.Properties['last_error_detail']
            $errorTimeProperty = $failureStatus.PSObject.Properties['last_error_at']
            if ($Action -eq 'Run' -and $null -ne $errorTimeProperty -and
                $null -ne $errorTimeProperty.Value -and
                [double]$errorTimeProperty.Value -ge $operationStartedSeconds -and
                $null -ne $detailProperty -and $null -ne $detailProperty.Value) {
                $codeProperty = $detailProperty.Value.PSObject.Properties['binance_code']
                if ($null -ne $codeProperty -and $codeProperty.Value -eq -2015) {
                    Write-Host 'Yetkilendirme reddedildi (-2015). VPN/ag degisince dis IP degisebilir.'
                    Write-Host 'Ayni API anahtarinin izinli IP listesini ve Enable Futures yetkisini kontrol edin.'
                    Write-Host 'Anahtar istemeyen baglanti kontrolu: bu scripti -Action NetworkCheck ile calistirin.'
                    Write-Host 'IP/yetki duzeltmesinden sonra -Action Diagnose ile dogrulayin.'
                }
            }
        }
        catch { Write-Host 'Ek yerel hata bilgisi okunamadi; yukaridaki asil hata gecerlidir.' }
        throw ('BTC Futures worker hata ile sonlandi (cikis kodu: {0}); Futures defterini kontrol edin.' -f $workerExit)
    }
}
finally {
    Remove-Item Env:BTC_FUTURES_MAINNET_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:BTC_FUTURES_MAINNET_SECRET_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:BTC_FUTURES_LIVE_AUTHORIZATION -ErrorAction SilentlyContinue
    Remove-Item Env:BTC_FUTURES_RISK_PROFILE -ErrorAction SilentlyContinue
    Remove-Item Env:BTC_FUTURES_ENTRY_MODE -ErrorAction SilentlyContinue
    Remove-Item Env:BTC_FUTURES_SIGNAL_PROFILE -ErrorAction SilentlyContinue
    $env:PYTHONIOENCODING = $previousEncoding
    $env:BTC_FUTURES_LEVERAGE = $previousLeverage
    $env:BTC_FUTURES_MODEL_DECISIONS = $previousModelDecisions
    if ($apiPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiPointer) }
    if ($secretPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer) }
    if ($null -ne $apiSecure) { $apiSecure.Dispose() }
    if ($null -ne $secretSecure) { $secretSecure.Dispose() }
}
