[CmdletBinding()]
param(
    [Parameter()]
    [string]$PythonPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$agentPath = Join-Path $projectRoot 'agent.py'
$confirmationPhrase = 'BINANCE TESTNET DEFTERINI ARSIVLE VE SIFIRLA'

function Resolve-PythonInvocation {
    param([string]$RequestedPath)
    if ($RequestedPath) {
        if (Test-Path -LiteralPath $RequestedPath -PathType Leaf) {
            return [pscustomobject]@{
                Executable = (Resolve-Path -LiteralPath $RequestedPath).Path
                PrefixArguments = @()
            }
        }
        $requested = Get-Command -Name $RequestedPath -CommandType Application `
            -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $requested) {
            return [pscustomobject]@{ Executable = $requested.Source; PrefixArguments = @() }
        }
        throw "Python bulunamadi: $RequestedPath"
    }
    $venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return [pscustomobject]@{ Executable = $venvPython; PrefixArguments = @() }
    }
    foreach ($name in @('python.exe', 'python')) {
        $command = Get-Command -Name $name -CommandType Application `
            -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $command) {
            return [pscustomobject]@{ Executable = $command.Source; PrefixArguments = @() }
        }
    }
    $launcher = Get-Command -Name 'py.exe' -CommandType Application `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $launcher) {
        return [pscustomobject]@{ Executable = $launcher.Source; PrefixArguments = @('-3') }
    }
    throw 'Python 3 bulunamadi. Betigi -PythonPath parametresiyle tekrar calistirin.'
}

function Invoke-AgentCommand {
    param([string[]]$AgentArguments)
    $commandOutput = @(& $script:pythonInvocation.Executable `
        @($script:pythonInvocation.PrefixArguments) `
        $script:agentPath `
        @AgentArguments 2>&1)
    $commandExitCode = $LASTEXITCODE
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

$pythonInvocation = Resolve-PythonInvocation -RequestedPath $PythonPath
$apiKeySecure = $null
$secretKeySecure = $null
[System.IntPtr]$apiKeyBstr = [System.IntPtr]::Zero
[System.IntPtr]$secretKeyBstr = [System.IntPtr]::Zero
$apiKeyPlain = $null
$secretKeyPlain = $null
$confirmation = $null

try {
    $statusOutput = & $pythonInvocation.Executable `
        @($pythonInvocation.PrefixArguments) `
        $agentPath `
        'binance-testnet-agent-status'
    if ($LASTEXITCODE -ne 0) { throw 'Testnet worker durumu okunamadi.' }
    $status = ($statusOutput -join "`n") | ConvertFrom-Json
    if ($status.running -or $status.desired_running) {
        throw 'Reset icin worker tamamen durmus ve desired_running=false olmalidir.'
    }
    if ($null -ne $status.pending_client_id) {
        throw 'Bekleyen emir niyeti varken reset yapilamaz.'
    }

    Write-Host 'Bu islem aktif Testnet karar/emir defterini ayni SQLite icinde arsivler.'
    Write-Host 'Arsiv kayitlari silinmez; yeni Testnet epoch nakit ve sifir pozisyonla baslar.'
    $confirmation = Read-Host "Devam etmek icin tam olarak '$confirmationPhrase' yazin"
    if ($confirmation -cne $confirmationPhrase) {
        Write-Warning 'Onay cumlesi eslesmedi. Reset yapilmadi.'
        return
    }

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
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_RESET_ENABLED', 'reset', 'Process')
    $apiKeyPlain = $null
    $secretKeyPlain = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiKeyBstr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretKeyBstr)
    $apiKeyBstr = [System.IntPtr]::Zero
    $secretKeyBstr = [System.IntPtr]::Zero

    Invoke-AgentCommand -AgentArguments @('binance-testnet-agent-reset')
}
finally {
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_API_KEY', $null, 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_SECRET_KEY', $null, 'Process')
    [Environment]::SetEnvironmentVariable('BINANCE_TESTNET_RESET_ENABLED', $null, 'Process')
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
