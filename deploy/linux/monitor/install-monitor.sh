#!/bin/bash
# Installs a read-only monitoring login: user `monitor`, whose SSH key can only run remora-monitor
# (forced command, no shell, no port/agent/X11 forwarding, no terminal). Run as root:
#   bash deploy/linux/monitor/install-monitor.sh /root/remora_monitor.pub
# Remove access again: rm /home/monitor/.ssh/authorized_keys
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
PUB=${1:?"kullanim: bash install-monitor.sh /root/remora_monitor.pub"}
[ "$(id -u)" -eq 0 ] || { echo "root olarak calistirin"; exit 1; }
grep -qE '^ssh-ed25519 [A-Za-z0-9+/=]+( |$)' "$PUB" || { echo "gecersiz ed25519 acik anahtar: $PUB"; exit 1; }

id monitor >/dev/null 2>&1 || useradd --create-home --shell /bin/sh monitor
passwd -l monitor >/dev/null                  # no password login, key only
install -m 755 "$HERE/remora-monitor" /usr/local/bin/remora-monitor
echo "monitor ALL=(root) NOPASSWD: /usr/local/bin/remora-monitor" > /etc/sudoers.d/remora-monitor
chmod 440 /etc/sudoers.d/remora-monitor
visudo -cf /etc/sudoers.d/remora-monitor >/dev/null

install -d -o monitor -g monitor -m 700 /home/monitor/.ssh
KEY=$(awk '{print $1" "$2}' "$PUB")
echo "command=\"sudo /usr/local/bin/remora-monitor\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty,no-user-rc $KEY remora-monitor" \
    > /home/monitor/.ssh/authorized_keys
chown monitor:monitor /home/monitor/.ssh/authorized_keys
chmod 600 /home/monitor/.ssh/authorized_keys
echo "Kurulum tamam. Test (Windows'ta): ssh -i \$env:USERPROFILE\\.ssh\\remora_monitor monitor@$(hostname -I | awk '{print $1}')"
