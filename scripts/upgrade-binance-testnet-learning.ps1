[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DataPath,

    [Parameter()]
    [string]$ManifestPath,

    [Parameter()]
    [ValidateRange(1, 1000000)]
    [int]$Samples,

    [Parameter()]
    [ValidateSet('15m', '1d')]
    [string]$Interval,

    [Parameter()]
    [string]$PythonPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$agentPath = Join-Path $projectRoot 'agent.py'
$confirmationPhrase = 'BINANCE TESTNET OGRENME SEEDINI UYGULA'

function Resolve-PythonInvocation {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        if (Test-Path -LiteralPath $RequestedPath -PathType Leaf) {
            return [pscustomobject]@{
                Executable = (Resolve-Path -LiteralPath $RequestedPath).Path
                PrefixArguments = @()
            }
        }
        $requestedCommand = Get-Command -Name $RequestedPath -CommandType Application `
            -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $requestedCommand) {
            return [pscustomobject]@{
                Executable = $requestedCommand.Source
                PrefixArguments = @()
            }
        }
        throw "Python bulunamadi: $RequestedPath"
    }

    $venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return [pscustomobject]@{ Executable = $venvPython; PrefixArguments = @() }
    }
    foreach ($commandName in @('python.exe', 'python')) {
        $pythonCommand = Get-Command -Name $commandName -CommandType Application `
            -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $pythonCommand) {
            return [pscustomobject]@{
                Executable = $pythonCommand.Source
                PrefixArguments = @()
            }
        }
    }
    $pyLauncher = Get-Command -Name 'py.exe' -CommandType Application `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $pyLauncher) {
        return [pscustomobject]@{ Executable = $pyLauncher.Source; PrefixArguments = @('-3') }
    }
    throw 'Python 3 bulunamadi. Betigi -PythonPath parametresiyle tekrar calistirin.'
}

function Invoke-AgentCommand {
    param([string[]]$AgentArguments)

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $commandOutput = @(& $script:pythonInvocation.Executable `
            @($script:pythonInvocation.PrefixArguments) `
            $script:agentPath `
            @AgentArguments 2>&1)
        $commandExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $commandOutput | ForEach-Object { Write-Host $_ }
    if ($commandExitCode -ne 0) {
        $detail = (($commandOutput | ForEach-Object { $_.ToString() }) -join "`n").Trim()
        if ($detail.Length -gt 700) {
            $detail = $detail.Substring($detail.Length - 700)
        }
        if (-not $detail) { $detail = 'Alt komut ayrinti dondurmedi.' }
        throw "Agent komutu basarisiz oldu (cikis kodu: $commandExitCode): $($AgentArguments -join ' ')`nAyrinti: $detail"
    }
    return ,$commandOutput
}

function Invoke-AgentJson {
    param([string[]]$AgentArguments)

    $output = Invoke-AgentCommand -AgentArguments $AgentArguments
    try {
        return (($output | ForEach-Object { $_.ToString() }) -join "`n") |
            ConvertFrom-Json
    }
    catch {
        throw "Agent JSON ciktisi okunamadi: $($AgentArguments -join ' ')"
    }
}

if (-not (Test-Path -LiteralPath $agentPath -PathType Leaf)) {
    throw "agent.py bulunamadi: $agentPath"
}
$resolvedDataPath = (Resolve-Path -LiteralPath $DataPath -ErrorAction Stop).Path
$resolvedManifestPath = $null
if ($ManifestPath) {
    $resolvedManifestPath = (
        Resolve-Path -LiteralPath $ManifestPath -ErrorAction Stop
    ).Path
}
$pythonInvocation = Resolve-PythonInvocation -RequestedPath $PythonPath

$seedArguments = @('binance-testnet-seed-learning', '--data', $resolvedDataPath)
if ($resolvedManifestPath) {
    $seedArguments += @('--manifest', $resolvedManifestPath)
}
if ($PSBoundParameters.ContainsKey('Samples')) {
    $seedArguments += @('--samples', $Samples.ToString())
}
if ($Interval) {
    $seedArguments += @('--interval', $Interval)
}
[object[]]$upgradeArguments = $seedArguments.Clone()
$upgradeArguments[0] = 'binance-testnet-upgrade-learning'

$apiKeySecure = $null
$secretKeySecure = $null
[System.IntPtr]$apiKeyBstr = [System.IntPtr]::Zero
[System.IntPtr]$secretKeyBstr = [System.IntPtr]::Zero
$apiKeyPlain = $null
$secretKeyPlain = $null
$previousPythonIoEncoding = [Environment]::GetEnvironmentVariable(
    'PYTHONIOENCODING', 'Process'
)
$previousPythonUtf8 = [Environment]::GetEnvironmentVariable('PYTHONUTF8', 'Process')

try {
    [Environment]::SetEnvironmentVariable('PYTHONIOENCODING', 'utf-8', 'Process')
    [Environment]::SetEnvironmentVariable('PYTHONUTF8', '1', 'Process')
    Write-Host '[1/4] Tarihsel seed girdisi salt okunur dogrulaniyor'
    [void](Invoke-AgentCommand -AgentArguments ($seedArguments + '--validate-only'))

    Write-Host "`n[2/4] Mevcut worker durumu okunuyor"
    $initialStatus = Invoke-AgentJson -AgentArguments @(
        'binance-testnet-agent-status'
    )
    if (-not $initialStatus.status_available) {
        throw 'Worker durumu guvenilir bicimde okunamadi.'
    }
    if ($initialStatus.pending_client_id) {
        throw 'Bekleyen bir emir niyeti varken surum gecisi yapilamaz.'
    }
    if ([bool]$initialStatus.running -ne [bool]$initialStatus.desired_running) {
        throw 'Worker bir start/stop gecisinde. Gecis tamamlandiktan sonra tekrar deneyin.'
    }
    Write-Host "`n[3/4] Testnet anahtari ve VPN baglantisi dogrulaniyor"
    $apiKeySecure = Read-Host 'Binance Spot Testnet API key' -AsSecureString
    if ($apiKeySecure.Length -eq 0) { throw 'API key bos olamaz.' }
    $secretKeySecure = Read-Host 'Binance Spot Testnet secret key' -AsSecureString
    if ($secretKeySecure.Length -eq 0) { throw 'Secret key bos olamaz.' }

    $apiKeyBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiKeySecure)
    $secretKeyBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secretKeySecure)
    $apiKeyPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiKeyBstr)
    $secretKeyPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretKeyBstr)
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_API_KEY', $apiKeyPlain, 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_SECRET_KEY', $secretKeyPlain, 'Process')
    $apiKeyPlain = $null
    $secretKeyPlain = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiKeyBstr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretKeyBstr)
    $apiKeyBstr = [System.IntPtr]::Zero
    $secretKeyBstr = [System.IntPtr]::Zero

    [void](Invoke-AgentCommand -AgentArguments @('binance-execution-doctor'))
    [void](Invoke-AgentCommand -AgentArguments @('binance-testnet-account'))
    [void](Invoke-AgentCommand -AgentArguments @(
        'binance-testnet-bound-account-check'
    ))
    [void](Invoke-AgentCommand -AgentArguments @(
        'binance-testnet-order-check', '--quote-usdt', '10'
    ))

    $confirmation = Read-Host "Devam etmek icin tam olarak '$confirmationPhrase' yazin"
    if ($confirmation -cne $confirmationPhrase) {
        Write-Warning 'Onay cumlesi eslesmedi. Worker durdurulmadi ve seed yazilmadi.'
        return
    }

    # Network checks and the interactive prompt can take an arbitrary amount
    # of time. Re-read lifecycle intent immediately before mutation so a
    # concurrent operator stop is never undone from this script's stale view.
    $preStopStatus = Invoke-AgentJson -AgentArguments @(
        'binance-testnet-agent-status'
    )
    if (
        -not $preStopStatus.status_available -or
        [bool]$preStopStatus.running -ne [bool]$initialStatus.running -or
        [bool]$preStopStatus.desired_running -ne [bool]$initialStatus.desired_running -or
        [string]$preStopStatus.pending_client_id -cne [string]$initialStatus.pending_client_id -or
        [string]$preStopStatus.policy -cne [string]$initialStatus.policy -or
        [string]$preStopStatus.policy_model_version -cne [string]$initialStatus.policy_model_version -or
        [bool]$preStopStatus.account_bound -ne [bool]$initialStatus.account_bound
    ) {
        throw 'Worker durumu onay beklenirken degisti. Hicbir stop/seed islemi uygulanmadi; guncel durumla tekrar deneyin.'
    }

    [Environment]::SetEnvironmentVariable('BINANCE_ORDER_EXECUTION_ENABLED', 'testnet', 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_WORKER_ENABLED', 'true', 'Process')

    Write-Host "`n[4/4] Worker tek kontrol kilidi altinda durduruluyor, seed kuruluyor ve baslangic calisma niyeti geri yukleniyor"
    [void](Invoke-AgentCommand -AgentArguments $upgradeArguments)
    [void](Invoke-AgentCommand -AgentArguments @('binance-testnet-agent-status'))
}
catch {
    throw
}
finally {
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_API_KEY', $null, 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_SECRET_KEY', $null, 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_ORDER_EXECUTION_ENABLED', $null, 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_WORKER_ENABLED', $null, 'Process')
    [Environment]::SetEnvironmentVariable(
        'PYTHONIOENCODING', $previousPythonIoEncoding, 'Process'
    )
    [Environment]::SetEnvironmentVariable('PYTHONUTF8', $previousPythonUtf8, 'Process')
    $apiKeyPlain = $null
    $secretKeyPlain = $null
    if ($apiKeyBstr -ne [System.IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiKeyBstr)
    }
    if ($secretKeyBstr -ne [System.IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretKeyBstr)
    }
    if ($null -ne $apiKeySecure) { $apiKeySecure.Dispose() }
    if ($null -ne $secretKeySecure) { $secretKeySecure.Dispose() }
}
