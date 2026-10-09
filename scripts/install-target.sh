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
apt-get install -y python3 python3-venv python3-pip

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home /nonexistent --shell /usr/sbin/nologin "$SERVICE_USER"
fi

mkdir -p "$APP_DIR"
if [[ "$SOURCE_DIR" != "$APP_DIR" ]]; then
  rm -rf "$APP_DIR"/*
  cp -a "$SOURCE_DIR"/. "$APP_DIR"/
fi
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
chown -R root:root "$APP_DIR"

install -m 0644 "$APP_DIR/deploy/systemd/netem-traffic-target.service" /etc/systemd/system/netem-traffic-target.service
systemctl daemon-reload
systemctl enable --now netem-traffic-target

echo "Controlled target installed on HTTP/8090 and UDP/9000."
