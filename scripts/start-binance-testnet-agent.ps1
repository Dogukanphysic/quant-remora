[CmdletBinding()]
param(
    [Parameter()]
    [string]$PythonPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$agentPath = Join-Path $projectRoot 'agent.py'
$confirmationPhrase = 'BINANCE TESTNET AGENTI BASLAT'

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

    & $script:pythonInvocation.Executable `
        @($script:pythonInvocation.PrefixArguments) `
        $script:agentPath `
        @AgentArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Agent komutu basarisiz oldu (cikis kodu: $LASTEXITCODE): $($AgentArguments -join ' ')"
    }
}

if (-not (Test-Path -LiteralPath $agentPath -PathType Leaf)) {
    throw "agent.py bulunamadi: $agentPath"
}

$pythonInvocation = Resolve-PythonInvocation -RequestedPath $PythonPath
$apiKeySecure = $null
$secretKeySecure = $null
[System.IntPtr]$apiKeyBstr = [System.IntPtr]::Zero
[System.IntPtr]$secretKeyBstr = [System.IntPtr]::Zero
$apiKeyPlain = $null
$secretKeyPlain = $null
$confirmation = $null

try {
    Write-Host 'Bu betik yalniz Binance Spot Testnet background agentini baslatir.'
    Write-Host 'Agent, sinyal degisimlerinde 10 USDT sanal BTCUSDT emirleri gonderebilir.'
    Write-Host 'Gercek Binance endpointleri ve gercek para desteklenmez.'

    $statusOutput = & $pythonInvocation.Executable `
        @($pythonInvocation.PrefixArguments) `
        $agentPath `
        'binance-testnet-agent-status'
    if ($LASTEXITCODE -ne 0) {
        throw 'Mevcut Binance Testnet worker durumu okunamadi.'
    }
    $currentStatus = ($statusOutput -join "`n") | ConvertFrom-Json
    if ($currentStatus.running) {
        throw 'Binance Testnet worker zaten calisiyor. Anahtar degistirmek icin once binance-testnet-agent-stop kullanin.'
    }

    [Environment]::SetEnvironmentVariable('BINANCE_ORDER_EXECUTION_ENABLED', $null, 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_WORKER_ENABLED', $null, 'Process')

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

    Write-Host "`n[1/3] Public Testnet baglantisi"
    Invoke-AgentCommand -AgentArguments @('binance-execution-doctor')
    Write-Host "`n[2/3] Imzali hesap ve acik emir uzlastirmasi"
    Invoke-AgentCommand -AgentArguments @('binance-testnet-account')
    Write-Host "`n[3/3] Emir olusturmadan 10 USDT parametre kontrolu"
    Invoke-AgentCommand -AgentArguments @('binance-testnet-order-check', '--quote-usdt', '10')

    Write-Host "`nOn kontroller basarili. Background agent gelecekte sanal Testnet emirleri olusturabilir."
    $confirmation = Read-Host "Devam etmek icin tam olarak '$confirmationPhrase' yazin"
    if ($confirmation -cne $confirmationPhrase) {
        Write-Warning 'Onay cumlesi eslesmedi. Testnet agenti baslatilmadi.'
        return
    }

    [Environment]::SetEnvironmentVariable('BINANCE_ORDER_EXECUTION_ENABLED', 'testnet', 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_WORKER_ENABLED', 'true', 'Process')
    Invoke-AgentCommand -AgentArguments @('binance-testnet-agent-start')
    Invoke-AgentCommand -AgentArguments @('binance-testnet-agent-status')
}
finally {
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_API_KEY', $null, 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_SECRET_KEY', $null, 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_ORDER_EXECUTION_ENABLED', $null, 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_WORKER_ENABLED', $null, 'Process')

    $apiKeyPlain = $null
    $secretKeyPlain = $null
    $confirmation = $null
    if ($apiKeyBstr -ne [System.IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiKeyBstr)
    }
    if ($secretKeyBstr -ne [System.IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretKeyBstr)
    }
    if ($null -ne $apiKeySecure) { $apiKeySecure.Dispose() }
    if ($null -ne $secretKeySecure) { $secretKeySecure.Dispose() }
}
