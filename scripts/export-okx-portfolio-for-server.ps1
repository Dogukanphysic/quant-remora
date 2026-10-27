# Packs the committed okx-agent code (git archive HEAD, read-only on that repo; local edits are NOT included)
# into state\server-export\okx-portfolio-code.tar for deploy/linux/okx-portfolio/setup-okx-portfolio.sh.
# No ledger is copied: on its own Demo account the portfolio bot starts with a fresh ledger.
param([string]$OkxAgent = (Join-Path $env:USERPROFILE "Documents\Codex\2026-09-12\yerasdasd\outputs\okx-agent"))
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "state\server-export"
if (-not (Test-Path (Join-Path $OkxAgent "futures_testnet_portfolio.py"))) { throw "okx-agent bulunamadi: $OkxAgent" }
New-Item -ItemType Directory -Force $out | Out-Null
git -C $OkxAgent archive --format=tar -o (Join-Path $out "okx-portfolio-code.tar") HEAD
if ($LASTEXITCODE -ne 0) { throw "git archive basarisiz" }
Write-Host "Hazir: $out\okx-portfolio-code.tar (commit $(git -C $OkxAgent rev-parse --short HEAD))"
Write-Host "Sunucuya kopyalayin:  scp `"$out\okx-portfolio-code.tar`" `"$out\remora-code.tar`" root@2.59.119.31:/root/"
