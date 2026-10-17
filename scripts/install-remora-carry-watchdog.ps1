# One-time setup of the carry bot watchdog (virtual money, Futures Demo only).
# Saves the Demo API keys encrypted with Windows DPAPI (readable only by this Windows user on this PC) in
# state\carry-keys.dpapi and registers a scheduled task that runs remora-carry-watchdog.ps1 every 5 minutes
# while you are logged on. Remove with: -Uninstall
param([ValidateSet("demo", "testnet")][string]$Environment = "demo", [ValidateSet("a", "b")][string]$Universe = "b",
      [ValidateRange(500, 10000)][int]$GrossUsdt = 4000, [switch]$Uninstall)
$root = Split-Path -Parent $PSScriptRoot
$keys = Join-Path $root "state\carry-keys.dpapi"
$task = "RemoraCarryWatchdog"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item $keys -ErrorAction SilentlyContinue
    Write-Host "Watchdog kaldirildi, kayitli anahtarlar silindi."
    exit 0
}
$key = Read-Host "Demo API key" -AsSecureString
$secret = Read-Host "Demo Secret key" -AsSecureString
[pscustomobject]@{
    key = ConvertFrom-SecureString $key; secret = ConvertFrom-SecureString $secret
    environment = $Environment; universe = $Universe; gross = $GrossUsdt
} | ConvertTo-Json | Set-Content -Path $keys -Encoding utf8

$script = Join-Path $PSScriptRoot "remora-carry-watchdog.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Restarts the Remora carry bot (Binance Futures Demo, virtual money) if it stopped." -Force | Out-Null
Write-Host "Watchdog kuruldu: 5 dakikada bir kontrol. Anahtarlar sifreli: $keys"
Write-Host "Durum gunlugu: state\remora-carry-watchdog.log  |  Kaldirmak: install-remora-carry-watchdog.ps1 -Uninstall"
