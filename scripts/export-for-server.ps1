# Prepares the two files the Linux server needs, in state\server-export\:
#   remora-code.tar                       the committed code (git archive HEAD; local edits are NOT included)
#   remora-bot-carry-testnet-b.sqlite3    a consistent copy of the carry ledger (SQLite backup, no keys inside)
# The carry bot must be stopped first, so the ledger does not change after the copy:
#   powershell -File scripts\stop-remora-bot.ps1 -Mode carry
param([string]$PythonPath = "python")
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "state\server-export"
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "remora_bot\s+run-carry" }
if ($running) { throw "Carry botu hala calisiyor (PID $($running.ProcessId)). Once durdurun: scripts\stop-remora-bot.ps1 -Mode carry" }
if (git -C $root status --porcelain) { Write-Host "Uyari: commit edilmemis degisiklikler var; sadece commit edilmis kod aktarilir." }

New-Item -ItemType Directory -Force $out | Out-Null
git -C $root archive --format=tar -o (Join-Path $out "remora-code.tar") HEAD
if ($LASTEXITCODE -ne 0) { throw "git archive basarisiz" }
$src = Join-Path $root "state\remora-bot-carry-testnet-b.sqlite3"
$dst = Join-Path $out "remora-bot-carry-testnet-b.sqlite3"
Remove-Item $dst -ErrorAction SilentlyContinue
& $PythonPath -c "import sqlite3, sys; s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" $src $dst
if ($LASTEXITCODE -ne 0) { throw "kayit kopyalanamadi" }
Write-Host "Hazir: $out"
Write-Host "Sunucuya kopyalayin:  scp `"$out\remora-code.tar`" `"$dst`" root@SUNUCU_IP:/root/"
Write-Host "Sunucuda:  tar -xf remora-code.tar deploy/linux/setup-server.sh && sudo bash deploy/linux/setup-server.sh remora-code.tar remora-bot-carry-testnet-b.sqlite3"
Write-Host "Windows'ta botun tekrar baslamamasi icin bekciyi kaldirin:  scripts\install-remora-carry-watchdog.ps1 -Uninstall"
