# Runs from any current directory; stop on download failure.
& python (Join-Path $PSScriptRoot 'agent.py') download --candles 1000
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& python (Join-Path $PSScriptRoot 'agent.py') research
exit $LASTEXITCODE
