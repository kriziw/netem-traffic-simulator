#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/netem-traffic-simulator
SERVICE_USER=trafficgen-target

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

apt-get update
apt-get install -y python3 python3-venv python3-pip python3-dev build-essential iproute2 rsync

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home /nonexistent --shell /usr/sbin/nologin "$SERVICE_USER"
fi

mkdir -p "$APP_DIR"
systemctl stop netem-traffic-target 2>/dev/null || true
if [[ "$SOURCE_DIR" != "$APP_DIR" ]]; then
  rsync -a --delete --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache "$SOURCE_DIR"/ "$APP_DIR"/
fi
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/pip" check
chown -R root:root "$APP_DIR"

install -m 0644 "$APP_DIR/deploy/systemd/netem-traffic-target-address.service" /etc/systemd/system/netem-traffic-target-address.service
install -m 0644 "$APP_DIR/deploy/systemd/netem-traffic-target.service" /etc/systemd/system/netem-traffic-target.service
bash "$APP_DIR/scripts/configure-lxc-service.sh" netem-traffic-target
systemctl daemon-reload
systemctl enable --now netem-traffic-target-address
systemctl enable netem-traffic-target
systemctl restart netem-traffic-target
systemctl is-active --quiet netem-traffic-target

echo "Controlled target installed."
echo "Synthetic target IP: 198.18.0.1/32"
echo "HTTP: http://198.18.0.1:8090"
echo "UDP media sink: 198.18.0.1:9000"
