[CmdletBinding()]
param(
    [Parameter()]
    [string]$PythonPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$agentPath = Join-Path $projectRoot 'agent.py'

function Resolve-PythonInvocation {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        if (Test-Path -LiteralPath $RequestedPath -PathType Leaf) {
            return [pscustomobject]@{
                Executable = (Resolve-Path -LiteralPath $RequestedPath).Path
                PrefixArguments = @()
            }
        }

        $requestedCommand = Get-Command -Name $RequestedPath -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
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
        return [pscustomobject]@{
            Executable = $venvPython
            PrefixArguments = @()
        }
    }

    foreach ($commandName in @('python.exe', 'python')) {
        $pythonCommand = Get-Command -Name $commandName -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $pythonCommand) {
            return [pscustomobject]@{
                Executable = $pythonCommand.Source
                PrefixArguments = @()
            }
        }
    }

    $pyLauncher = Get-Command -Name 'py.exe' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $pyLauncher) {
        return [pscustomobject]@{
            Executable = $pyLauncher.Source
            PrefixArguments = @('-3')
        }
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
        if ($detail.Length -gt 500) {
            $detail = $detail.Substring($detail.Length - 500)
        }
        if (-not $detail) { $detail = 'Alt komut ayrinti dondurmedi.' }
        throw "Agent komutu basarisiz oldu (cikis kodu: $commandExitCode): $($AgentArguments -join ' ')`nAyrinti: $detail"
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

try {
    Write-Host 'Binance Spot Testnet kimlik bilgileri yalniz bu PowerShell isleminin belleginde tutulur.'
    Write-Host 'Bu betik gercek veya sanal emir gondermez; son adim yalniz /api/v3/order/test dogrulamasidir.'

    $apiKeySecure = Read-Host 'Binance Spot Testnet API key' -AsSecureString
    if ($apiKeySecure.Length -eq 0) {
        throw 'API key bos olamaz.'
    }

    $secretKeySecure = Read-Host 'Binance Spot Testnet secret key' -AsSecureString
    if ($secretKeySecure.Length -eq 0) {
        throw 'Secret key bos olamaz.'
    }

    $apiKeyBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiKeySecure)
    $secretKeyBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secretKeySecure)
    $apiKeyPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiKeyBstr)
    $secretKeyPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretKeyBstr)

    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_API_KEY', $apiKeyPlain, 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_SECRET_KEY', $secretKeyPlain, 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_ORDER_EXECUTION_ENABLED', 'false', 'Process')

    Write-Host "`n[1/3] Public Testnet baglantisi ve BTCUSDT filtreleri"
    Invoke-AgentCommand -AgentArguments @('binance-execution-doctor')

    Write-Host "`n[2/3] Imzali hesap ve acik emir uzlastirmasi"
    Invoke-AgentCommand -AgentArguments @('binance-testnet-account')

    Write-Host "`n[3/3] 5 USDT MARKET parametreleriyle /order/test"
    Invoke-AgentCommand -AgentArguments @('binance-testnet-order-check', '--quote-usdt', '5')

    Write-Host "`nDogrulama tamamlandi. /order/test gercek veya Testnet emri olusturmadi."
}
finally {
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_API_KEY', $null, 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_SECRET_KEY', $null, 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_ORDER_EXECUTION_ENABLED', $null, 'Process')

    $apiKeyPlain = $null
    $secretKeyPlain = $null

    if ($apiKeyBstr -ne [System.IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiKeyBstr)
        $apiKeyBstr = [System.IntPtr]::Zero
    }
    if ($secretKeyBstr -ne [System.IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretKeyBstr)
        $secretKeyBstr = [System.IntPtr]::Zero
    }

    if ($null -ne $apiKeySecure) {
        $apiKeySecure.Dispose()
    }
    if ($null -ne $secretKeySecure) {
        $secretKeySecure.Dispose()
    }
}
