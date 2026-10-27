#!/bin/sh
# Started by systemd (remora-carry.service). Same rule as the Windows watchdog: a halted ledger is never
# restarted automatically. Exiting 0 here leaves the service stopped until a person runs `remora-carry start`.
set -eu
PY=/opt/remora/venv/bin/python
cd /opt/remora/app
if ! "$PY" -m remora_bot carry-status | "$PY" -c 'import json, sys; sys.exit(3 if json.load(sys.stdin)["bot"]["halted"] else 0)'
then
    echo "ledger HALTED (or status unreadable); not starting. Inspect, then: sudo remora-carry start"
    exit 0
fi
exec "$PY" -u -m remora_bot run-carry
