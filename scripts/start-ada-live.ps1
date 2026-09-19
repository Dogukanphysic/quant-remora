[CmdletBinding()]
param([string]$PythonPath = 'python', [switch]$CheckOnly, [switch]$NewKey,
      [ValidateSet('4h','15m')][string]$Interval = '4h', [switch]$MigrateInterval,
      [switch]$ModelDecisions, [switch]$ReconcileOnly, [switch]$OrderCheckOnly,
      [switch]$RecoverUnsentOnly, [switch]$UseAllAllocatedFunds)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$apiSecure = $null
$secretSecure = $null
$apiPointer = [IntPtr]::Zero
$secretPointer = [IntPtr]::Zero
$previousEncoding = $env:PYTHONIOENCODING
if ($RecoverUnsentOnly -and ($CheckOnly -or $ReconcileOnly -or $OrderCheckOnly -or $NewKey -or $MigrateInterval -or $ModelDecisions)) {
    throw 'RecoverUnsentOnly yalniz Interval/PythonPath ile kullanilir.'
}
if ($OrderCheckOnly -and ($CheckOnly -or $ReconcileOnly -or $NewKey -or $MigrateInterval -or $ModelDecisions)) {
    throw 'OrderCheckOnly yalniz Interval/PythonPath ile kullanilir; gercek emir gondermez.'
}
if ($ReconcileOnly -and ($CheckOnly -or $NewKey -or $MigrateInterval -or $ModelDecisions)) {
    throw 'ReconcileOnly yalniz Interval/PythonPath ile kullanilir; emir gondermez.'
}
$modelArgs = @()
if ($UseAllAllocatedFunds) { $modelArgs += '--use-all-allocated-funds' }
if ($ModelDecisions) {
    if ($Interval -ne '15m' -or $CheckOnly) { throw 'ModelDecisions yalniz -Interval 15m ile baslatilir.' }
    $modelArgs += '--model-decisions'
}
if ($MigrateInterval -and ($NewKey -or $CheckOnly -or $Interval -ne '15m')) {
    throw 'Gecis icin -Interval 15m -MigrateInterval kullanin; NewKey/CheckOnly eklemeyin.'
}
try {
    if ($CheckOnly -or $ReconcileOnly -or $OrderCheckOnly -or $RecoverUnsentOnly) {
        Write-Host 'EMIR GONDERILMEZ; durmus agent yeniden baslatilmaz.'
        if ($RecoverUnsentOnly) { Write-Host 'Ilk HTTP 401 emrinin yoklugu ve dolum olmamasi dogrulanirsa kayit arsivlenir; yeni baslatma ayridir.' }
        if ($OrderCheckOnly) { Write-Host 'Binance order/test: islem yetkisi ve parametre kontrolu; gercek emir/defter degisikligi yok.' }
        if ($ReconcileOnly) { Write-Host 'Bekleyen emir borsadan sorgulanir; bulunan dolumlar yerel defterle uzlastirilir. Halt kaldirilmaz.' }
        $confirmation = '294 ADA ILE GERCEK ISLEM BASLAT'
    }
    else {
    Write-Host 'BINANCE MAINNET: Bu betigi calistirmaniz GERCEK ADA/USDT emirleri baslatabilir.'
    Write-Host '294 serbest ADA ayrilir. Hesaptaki diger USDT kullanilmaz. Tum ayrilan sermaye risk altindadir.'
    if ($UseAllAllocatedFunds) { Write-Host 'Tahsisli USDT ve kazanclarin tamami kullanilabilir; sonraki alimlarda 294 ADA adet tavani yoktur. Baska hesap bakiyeleri eklenmez.' }
    Write-Host "Ilk dongude satis olabilir. $Interval mum stratejisi, 60 saniye kontrol. Kar garantisi yoktur."
    if ($ModelDecisions) { Write-Host 'DENEYSEL MODEL KARARLARI: Karlilik dogrulanmadi. Pozitif tahmin AL/TUT, diger tahmin SAT/NAKIT. Stop/hedef onceliklidir.' }
    if ($MigrateInterval) { Write-Host 'Eski pencereyi Ctrl+C ile durdurun. Bakiye ve mevcut stop/hedef korunarak 15m gecisi yapilir.' }
    Write-Host 'BNB ile komisyon odemesi bu sembolde kapali olmali. Para cekme yetkisi gerekli degildir.'
    Write-Host 'Durdurmak icin Ctrl+C. Durdurma eldeki ADA yi satmaz; bilgisayar kapaliyken stop calismaz.'
    $confirmation = Read-Host 'Devam icin tam olarak 294 ADA ILE GERCEK ISLEM BASLAT yazin'
    if ($confirmation -cne '294 ADA ILE GERCEK ISLEM BASLAT') {
        throw 'Onay eslesmedi; hicbir islem baslatilmadi.'
    }
    }
    $apiSecure = Read-Host 'Binance MAINNET API key (Testnet degil)' -AsSecureString
    $secretSecure = Read-Host 'Binance MAINNET secret key' -AsSecureString
    if ($apiSecure.Length -eq 0 -or $secretSecure.Length -eq 0) { throw 'Anahtar bos olamaz.' }
    $apiPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiSecure)
    $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secretSecure)
    $env:ADA_MAINNET_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiPointer)
    $env:ADA_MAINNET_SECRET_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiPointer); $apiPointer = [IntPtr]::Zero
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer); $secretPointer = [IntPtr]::Zero
    $env:ADA_LIVE_AUTHORIZATION = $confirmation
    $env:PYTHONIOENCODING = 'utf-8'
    if ($CheckOnly) {
        & $PythonPath (Join-Path $projectRoot 'ada_live.py') diagnose --live --interval $Interval
    }
    elseif ($ReconcileOnly) {
        & $PythonPath (Join-Path $projectRoot 'ada_live.py') reconcile --live --interval $Interval @modelArgs
    }
    elseif ($OrderCheckOnly) {
        & $PythonPath (Join-Path $projectRoot 'ada_live.py') order-check --live --interval $Interval
    }
    elseif ($RecoverUnsentOnly) {
        & $PythonPath (Join-Path $projectRoot 'ada_live.py') recover-unsent --live --interval $Interval
    }
    else {
        if ($NewKey) {
            & $PythonPath (Join-Path $projectRoot 'ada_live.py') run --live --new-key --interval $Interval @modelArgs
        }
        elseif ($MigrateInterval) {
            & $PythonPath (Join-Path $projectRoot 'ada_live.py') run --live --interval $Interval --migrate-interval @modelArgs
        }
        else {
            & $PythonPath (Join-Path $projectRoot 'ada_live.py') run --live --interval $Interval @modelArgs
        }
    }
    if ($LASTEXITCODE -ne 0) { throw 'ADA worker hata ile sonlandi; durum kaydini kontrol edin.' }
}
finally {
    Remove-Item Env:ADA_MAINNET_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:ADA_MAINNET_SECRET_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:ADA_LIVE_AUTHORIZATION -ErrorAction SilentlyContinue
    $env:PYTHONIOENCODING = $previousEncoding
    if ($apiPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiPointer) }
    if ($secretPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer) }
    if ($null -ne $apiSecure) { $apiSecure.Dispose() }
    if ($null -ne $secretSecure) { $secretSecure.Dispose() }
}
