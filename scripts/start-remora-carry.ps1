# Switch the Demo/Testnet account from the exploration bot to the funding carry bot (virtual money).
# 1) stops the running bot, 2) closes the exploration bot's own positions through its ledger,
# 3) starts the carry bot detached. Keys are asked once, kept only in this process and the bot.
# Universe b (default) avoids the top-20 coins another bot on the shared Demo account trades.
param([string]$PythonPath = "python", [ValidateSet("demo", "testnet")][string]$Environment = "demo",
      [ValidateSet("a", "b")][string]$Universe = "b", [switch]$Fixed)
# Default: self-updating carry (weights re-learned weekly, carry_adaptive.py). -Fixed: the single fixed config.
$root = Split-Path -Parent $PSScriptRoot
$confirm = "REMORA CARRY DEMO BASLAT"

function Read-Secret([string]$prompt) {
    $secure = Read-Host $prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

foreach ($m in @("testnet", "carry")) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "stop-remora-bot.ps1") -Mode $m | Out-Null
}
Start-Sleep -Seconds 2
$env:REMORA_FUTURES_TESTNET_API_KEY = Read-Secret "API key"
$env:REMORA_FUTURES_TESTNET_SECRET_KEY = Read-Secret "Secret key"
$env:REMORA_FUTURES_TEST_ENV = $Environment
$env:REMORA_UNIVERSE = $Universe
$env:REMORA_CARRY_ADAPTIVE = if ($Fixed) { "false" } else { "true" }
try {
    Write-Host "Not: Turkce harf kullanmadan yazin (BASLAT)."
    $answer = (Read-Host "Onay icin tam olarak yazin: $confirm").Trim()
    if ($answer -ne $confirm) { throw "Onay cumlesi eslesmedi; hicbir sey yapilmadi." }
    $env:REMORA_CARRY_CONFIRM = $confirm
    Push-Location $root
    & $PythonPath -m remora_bot carry-switch
    $ok = ($LASTEXITCODE -eq 0)
    Pop-Location
    if (-not $ok) { throw "Kesif pozisyonlari kapatilamadi; carry botu baslatilmadi." }
    $p = Start-Process -FilePath $PythonPath -ArgumentList @("-u", "-m", "remora_bot", "run-carry") `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $root "state\remora-bot-carry-testnet.out.log") `
        -RedirectStandardError (Join-Path $root "state\remora-bot-carry-testnet.err.log")
    Write-Host "Carry botu basladi. PID=$($p.Id). Durum: state\remora-bot-carry-testnet-status.json"
}
finally {
    Remove-Item Env:REMORA_FUTURES_TESTNET_API_KEY, Env:REMORA_FUTURES_TESTNET_SECRET_KEY, Env:REMORA_FUTURES_TEST_ENV, `
        Env:REMORA_CARRY_CONFIRM, Env:REMORA_UNIVERSE, Env:REMORA_CARRY_ADAPTIVE -ErrorAction SilentlyContinue
}
