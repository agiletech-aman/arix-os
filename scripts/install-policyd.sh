#!/usr/bin/env bash
# Run INSIDE THE VM as root:  sudo scripts/install-policyd.sh <your-login>
# Sets up the Policy Engine as a separate service with its own user.
#   aios-policy  runs the service; owns the sandbox and the audit log
#   aios-agent   user the AI will run as (can only use agent.sock)
#   aios-admin   group allowed to approve / kill (your login is added)
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
HUMAN="${1:?usage: sudo $0 <your-login>}"
id "$HUMAN" >/dev/null
SRC="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$SRC/aios/policy/daemon.py" ] || { echo "run from the arix-os repo"; exit 1; }

groupadd -f aios-agents
groupadd -f aios-admin
id aios-policy >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin aios-policy
id aios-agent  >/dev/null 2>&1 || useradd --create-home --shell /bin/bash -G aios-agents aios-agent
usermod -aG aios-admin "$HUMAN"

# code: root-owned, not writable by the AI or the service
rm -rf /opt/aios
install -d -m 0755 /opt/aios
cp -r "$SRC/aios" /opt/aios/aios
rm -rf /opt/aios/aios/tests /opt/aios/aios/__pycache__ /opt/aios/aios/*/__pycache__
chown -R root:root /opt/aios
chmod -R go-w /opt/aios

# policy: root-owned. The service can read it but cannot change it.
install -d -m 0755 -o root -g root /etc/aios
[ -f /etc/aios/policy.toml ] || install -m 0644 -o root -g root "$SRC/policy.example.toml" /etc/aios/policy.toml

install -d -m 0700 -o aios-policy -g aios-policy /var/lib/aios /var/lib/aios/sandbox
install -d -m 2750 -o aios-policy -g aios-admin /var/log/aios

install -m 0644 "$SRC/deploy/aios-policyd.service" /etc/systemd/system/aios-policyd.service
systemctl daemon-reload
systemctl enable --now aios-policyd
sleep 1
systemctl --no-pager --lines=5 status aios-policyd || true
echo
echo "Done. Log out and back in so '$HUMAN' picks up the aios-admin group."
