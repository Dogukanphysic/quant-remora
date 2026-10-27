#!/bin/bash
# One-time setup of the okx-agent Futures portfolio bot on the same server as the carry bot, fully separate:
# own user (okxbot), own folder (/opt/okx-portfolio), own key file, own service, and its OWN Demo account.
# It starts with a fresh ledger (the Windows ledger is bound to the old shared account's key).
# Usage (as root, in the folder where you copied okx-portfolio-code.tar and remora-code.tar):
#   tar -xf remora-code.tar deploy/linux/okx-portfolio
#   bash deploy/linux/okx-portfolio/setup-okx-portfolio.sh okx-portfolio-code.tar
# It does NOT start the bot: run `okx-portfolio start` afterwards.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CODE_TAR=${1:?"kullanim: bash setup-okx-portfolio.sh okx-portfolio-code.tar"}
CONFIRM="AYRI DEMO HESAP"
[ "$(id -u)" -eq 0 ] || { echo "root olarak calistirin"; exit 1; }
[ -f "$CODE_TAR" ] || { echo "dosya bulunamadi: $CODE_TAR"; exit 1; }
command -v python3 >/dev/null || { apt-get update -q; apt-get install -y -q python3; }

id okxbot >/dev/null 2>&1 || useradd --system --home-dir /opt/okx-portfolio --shell /usr/sbin/nologin okxbot
install -d -o root -g root -m 755 /opt/okx-portfolio /opt/okx-portfolio/app
tar -xf "$CODE_TAR" -C /opt/okx-portfolio/app
chown -R root:root /opt/okx-portfolio/app
install -d -o okxbot -g okxbot -m 750 /opt/okx-portfolio/app/state
install -m 644 "$HERE/check-separate-account.py" /opt/okx-portfolio/check-separate-account.py

if [ -f /etc/okx-portfolio/portfolio.env ]; then
    echo "/etc/okx-portfolio/portfolio.env zaten var; anahtarlar degistirilmedi"
else
    echo "AYRI Demo hesabin anahtarlari (carry botunun hesabi DEGIL):"
    KEY=; SECRET=
    while [ -z "$KEY" ]; do read -rsp "Demo API key (sag tikla yapistir): " KEY; echo; done
    while [ -z "$SECRET" ]; do read -rsp "Demo Secret key (sag tikla yapistir): " SECRET; echo; done
    if [ -f /etc/remora/carry.env ] && grep -qxF "REMORA_FUTURES_TESTNET_API_KEY=$KEY" /etc/remora/carry.env; then
        unset KEY SECRET; echo "Bu, carry botunun anahtari; kaydedilmedi. Ayri hesabin anahtarini girin."; exit 1
    fi
    read -rp "Ayri bir Demo hesap oldugunu onaylamak icin tam olarak yazin: $CONFIRM: " ANSWER
    [ "$ANSWER" = "$CONFIRM" ] || { unset KEY SECRET; echo "onay eslesmedi; kaydedilmedi"; exit 1; }
    install -d -o root -g root -m 700 /etc/okx-portfolio
    umask 077
    printf 'FUTURES_PORTFOLIO_TESTNET_API_KEY=%s\nFUTURES_PORTFOLIO_TESTNET_SECRET_KEY=%s\n' "$KEY" "$SECRET" \
        > /etc/okx-portfolio/portfolio.env
    unset KEY SECRET
    chmod 600 /etc/okx-portfolio/portfolio.env
fi

install -m 755 "$HERE/okx-portfolio" /usr/local/bin/okx-portfolio
install -m 644 "$HERE/okx-portfolio.service" /etc/systemd/system/okx-portfolio.service
systemctl daemon-reload
echo
echo "Kurulum tamam. Windows'taki portfoy botu DURDURULMUS olmali. Kontrol + baslatma:  okx-portfolio start"
