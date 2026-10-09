#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/netem-traffic-simulator
CONFIG_DIR=/etc/netem-traffic-simulator
RUNTIME_DIR=/var/lib/netem-traffic-simulator
SERVICE_USER=trafficgen

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

apt-get update
apt-get install -y python3 python3-venv python3-pip openssl ca-certificates

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home "$RUNTIME_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

mkdir -p "$APP_DIR" "$CONFIG_DIR" "$RUNTIME_DIR"
rm -rf "$APP_DIR"/*
cp -a "$SOURCE_DIR"/. "$APP_DIR"/

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

chown -R root:root "$APP_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$RUNTIME_DIR" "$CONFIG_DIR"
chmod 0750 "$CONFIG_DIR" "$RUNTIME_DIR"

if [[ ! -s "$CONFIG_DIR/api.key" ]]; then
  printf 'ntg_%s\n' "$(openssl rand -base64 36 | tr -d '/+=\n' | cut -c1-48)" > "$CONFIG_DIR/api.key"
fi
if [[ ! -s "$CONFIG_DIR/admin.password" ]]; then
  openssl rand -base64 18 | tr -d '\n' > "$CONFIG_DIR/admin.password"
  printf '\n' >> "$CONFIG_DIR/admin.password"
fi
if [[ ! -s "$CONFIG_DIR/session.secret" ]]; then
  openssl rand -base64 48 | tr -d '\n' > "$CONFIG_DIR/session.secret"
  printf '\n' >> "$CONFIG_DIR/session.secret"
fi
chmod 0600 "$CONFIG_DIR/api.key" "$CONFIG_DIR/admin.password" "$CONFIG_DIR/session.secret"
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR/api.key" "$CONFIG_DIR/admin.password" "$CONFIG_DIR/session.secret"

if [[ ! -s "$CONFIG_DIR/tls.crt" || ! -s "$CONFIG_DIR/tls.key" ]]; then
  HOSTNAME_FQDN="$(hostname -f 2>/dev/null || hostname)"
  PRIMARY_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  SAN="DNS:${HOSTNAME_FQDN}"
  if [[ -n "${PRIMARY_IP}" ]]; then SAN="${SAN},IP:${PRIMARY_IP}"; fi
  openssl req -x509 -newkey rsa:3072 -nodes -days 825 \
    -keyout "$CONFIG_DIR/tls.key" \
    -out "$CONFIG_DIR/tls.crt" \
    -subj "/CN=${HOSTNAME_FQDN}" \
    -addext "subjectAltName=${SAN}"
  chmod 0600 "$CONFIG_DIR/tls.key"
  chmod 0644 "$CONFIG_DIR/tls.crt"
  chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR/tls.key" "$CONFIG_DIR/tls.crt"
fi

install -m 0644 "$APP_DIR/deploy/systemd/netem-traffic-simulator.service" /etc/systemd/system/netem-traffic-simulator.service
systemctl daemon-reload
systemctl enable --now netem-traffic-simulator

echo
echo "NetEm Traffic Simulator installed."
echo "UI/API: https://${PRIMARY_IP:-$(hostname)}:8443"
echo "Discovery: UDP/47890"
echo
echo "Administrator password:"
cat "$CONFIG_DIR/admin.password"
echo
echo "API key:"
cat "$CONFIG_DIR/api.key"
echo
echo "The API key is also viewable/rotatable from API & Integration after signing in."
