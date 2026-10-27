#!/bin/bash
# One-time setup of the carry bot on an Ubuntu 24.04 server (virtual money, Binance Futures Demo only).
# Usage (as root, in the folder where you copied the two files from scripts/export-for-server.ps1):
#   tar -xf remora-code.tar deploy/linux/setup-server.sh
#   sudo bash deploy/linux/setup-server.sh remora-code.tar remora-bot-carry-testnet-b.sqlite3
# It installs Python, creates the system user `remora`, unpacks the code to /opt/remora/app, puts the ledger in
# /opt/remora/app/state, asks for the Demo API keys (hidden input, saved only in /etc/remora/carry.env, mode 600)
# and installs the systemd service. It does NOT start the bot: run `sudo remora-carry start` afterwards.
set -euo pipefail
CODE_TAR=${1:?"kullanim: sudo bash setup-server.sh remora-code.tar remora-bot-carry-testnet-b.sqlite3"}
LEDGER=${2:?"kullanim: sudo bash setup-server.sh remora-code.tar remora-bot-carry-testnet-b.sqlite3"}
CONFIRM="REMORA CARRY DEMO BASLAT"
[ "$(id -u)" -eq 0 ] || { echo "root olarak calistirin (sudo)"; exit 1; }
[ -f "$CODE_TAR" ] && [ -f "$LEDGER" ] || { echo "dosyalar bulunamadi: $CODE_TAR $LEDGER"; exit 1; }

apt-get update -q
apt-get install -y -q python3 python3-venv
timedatectl set-ntp true || true                  # signed API requests need a correct clock

id remora >/dev/null 2>&1 || useradd --system --home-dir /opt/remora --shell /usr/sbin/nologin remora
install -d -o root -g root -m 755 /opt/remora /opt/remora/app
tar -xf "$CODE_TAR" -C /opt/remora/app
chown -R root:root /opt/remora/app
install -d -o remora -g remora -m 750 /opt/remora/app/state
if [ -e /opt/remora/app/state/remora-bot-carry-testnet-b.sqlite3 ]; then
    echo "sunucuda zaten bir kayit var; uzerine yazilmadi (once eskisini kendiniz tasiyin)"
else
    install -o remora -g remora -m 640 "$LEDGER" /opt/remora/app/state/remora-bot-carry-testnet-b.sqlite3
fi

[ -x /opt/remora/venv/bin/python ] || python3 -m venv /opt/remora/venv
/opt/remora/venv/bin/pip install -q --upgrade pip
/opt/remora/venv/bin/pip install -q -r /opt/remora/app/requirements.txt

if [ -f /etc/remora/carry.env ]; then
    echo "/etc/remora/carry.env zaten var; anahtarlar degistirilmedi (yenilemek icin dosyayi silip tekrar calistirin)"
else
    KEY=; SECRET=
    while [ -z "$KEY" ]; do read -rsp "Demo API key (sag tikla yapistir): " KEY; echo; done
    while [ -z "$SECRET" ]; do read -rsp "Demo Secret key (sag tikla yapistir): " SECRET; echo; done
    read -rp "Onay icin tam olarak yazin: $CONFIRM: " ANSWER
    [ "$ANSWER" = "$CONFIRM" ] || { echo "onay cumlesi eslesmedi; anahtarlar kaydedilmedi"; exit 1; }
    install -d -o root -g root -m 700 /etc/remora
    umask 077
    cat > /etc/remora/carry.env <<EOF
REMORA_FUTURES_TESTNET_API_KEY=$KEY
REMORA_FUTURES_TESTNET_SECRET_KEY=$SECRET
REMORA_FUTURES_TEST_ENV=demo
REMORA_UNIVERSE=b
REMORA_CARRY_ADAPTIVE=true
REMORA_CARRY_GROSS_USDT=4000
REMORA_CARRY_CONFIRM=$CONFIRM
EOF
    unset KEY SECRET
    chmod 600 /etc/remora/carry.env
fi

chmod 755 /opt/remora/app/deploy/linux/run-carry.sh
install -m 755 /opt/remora/app/deploy/linux/remora-carry /usr/local/bin/remora-carry
install -m 644 /opt/remora/app/deploy/linux/remora-carry.service /etc/systemd/system/remora-carry.service
systemctl daemon-reload
echo
echo "Kurulum tamam. Windows'taki bot DURDURULMUS olmali. Baslatmak icin:  sudo remora-carry start"
