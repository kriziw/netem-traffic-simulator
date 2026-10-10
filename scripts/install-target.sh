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
apt-get install -y python3 python3-venv python3-pip python3-dev build-essential iproute2 rsync git openssl ca-certificates curl

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home /nonexistent --shell /usr/sbin/nologin "$SERVICE_USER"
fi

mkdir -p "$APP_DIR"
systemctl stop netem-traffic-target 2>/dev/null || true
systemctl stop netem-traffic-target-manager 2>/dev/null || true
if [[ "$SOURCE_DIR" != "$APP_DIR" ]]; then
  rsync -a --delete --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache "$SOURCE_DIR"/ "$APP_DIR"/
fi
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/pip" check
chown -R root:root "$APP_DIR"
# rsync -a also copies the source directory mode (mktemp sources are 0700).
# The unprivileged service must be able to enter its working directory.
chmod 0755 "$APP_DIR"

install -m 0644 "$APP_DIR/deploy/systemd/netem-traffic-target-address.service" /etc/systemd/system/netem-traffic-target-address.service
install -m 0644 "$APP_DIR/deploy/systemd/netem-traffic-target.service" /etc/systemd/system/netem-traffic-target.service
bash "$APP_DIR/scripts/configure-lxc-service.sh" netem-traffic-target
systemctl daemon-reload
systemctl enable --now netem-traffic-target-address
systemctl enable netem-traffic-target
systemctl restart netem-traffic-target
systemctl is-active --quiet netem-traffic-target

# Enable remote management once from the modem console, bound only to its management IP.
# Subsequent installs retain this setting, the API key and TLS certificate.
MANAGER_CONFIG=/etc/netem-traffic-target-manager
if [[ -n "${NETEM_TARGET_MANAGEMENT_HOST:-}" || -s "$MANAGER_CONFIG/manager.env" ]]; then
  MANAGER_USER=trafficgen-target-manager
  if ! id "$MANAGER_USER" >/dev/null 2>&1; then
    useradd --system --home /nonexistent --shell /usr/sbin/nologin "$MANAGER_USER"
  fi
  mkdir -p "$MANAGER_CONFIG" /var/lib/netem-traffic-target-manager /var/lib/netem-traffic-target-admin
  chown root:"$MANAGER_USER" "$MANAGER_CONFIG"
  chmod 0750 "$MANAGER_CONFIG"
  chown "$MANAGER_USER:$MANAGER_USER" /var/lib/netem-traffic-target-manager
  chmod 0750 /var/lib/netem-traffic-target-manager
  chown root:root /var/lib/netem-traffic-target-admin
  chmod 0755 /var/lib/netem-traffic-target-admin
  if [[ -n "${NETEM_TARGET_MANAGEMENT_HOST:-}" ]]; then
    MANAGER_IP=$(python3 -c 'import ipaddress,sys; a=ipaddress.IPv4Address(sys.argv[1]); (a.is_unspecified or a.is_multicast or a.is_loopback) and sys.exit("Choose a specific management IPv4"); print(a)' "$NETEM_TARGET_MANAGEMENT_HOST")
    ip -j -4 address show | python3 -c 'import json,sys; host=sys.argv[1]; rows=json.load(sys.stdin); any(a.get("local")==host for r in rows for a in r.get("addr_info",[])) or sys.exit("Management IPv4 address is not configured on this modem")' "$MANAGER_IP"
    printf 'NETEM_TARGET_MANAGER_HOST=%s\n' "$MANAGER_IP" > "$MANAGER_CONFIG/manager.env"
  fi
  chown root:"$MANAGER_USER" "$MANAGER_CONFIG/manager.env"
  chmod 0640 "$MANAGER_CONFIG/manager.env"
  CREATED_MANAGER_KEY=false
  if [[ ! -s "$MANAGER_CONFIG/api.key" ]]; then
    printf 'ntt_%s\n' "$(openssl rand -hex 32)" > "$MANAGER_CONFIG/api.key"
    CREATED_MANAGER_KEY=true
  fi
  if [[ ! -s "$MANAGER_CONFIG/tls.crt" || ! -s "$MANAGER_CONFIG/tls.key" ]]; then
    openssl req -x509 -newkey rsa:3072 -nodes -days 825 \
      -keyout "$MANAGER_CONFIG/tls.key" -out "$MANAGER_CONFIG/tls.crt" \
      -subj '/CN=netem-traffic-target-manager'
  fi
  chown root:"$MANAGER_USER" "$MANAGER_CONFIG/api.key" "$MANAGER_CONFIG/tls.key"
  chown root:"$MANAGER_USER" "$MANAGER_CONFIG/tls.crt"
  chmod 0640 "$MANAGER_CONFIG/api.key" "$MANAGER_CONFIG/tls.key"
  chmod 0644 "$MANAGER_CONFIG/tls.crt"
  for unit in netem-traffic-target-manager.service netem-traffic-target-admin.service netem-traffic-target-admin.path; do
    install -m 0644 "$APP_DIR/deploy/systemd/$unit" "/etc/systemd/system/$unit"
  done
  bash "$APP_DIR/scripts/configure-lxc-service.sh" netem-traffic-target-manager
  NETEM_ADMIN_ROLE=target python3 -I -c "import sys; sys.path.insert(0, '$APP_DIR'); from trafficgen.host_admin import main; main()" --record-install
  systemctl daemon-reload
  systemctl enable --now netem-traffic-target-admin.path
  systemctl enable netem-traffic-target-manager
  systemctl restart netem-traffic-target-manager
  systemctl is-active --quiet netem-traffic-target-manager
  echo "Target update API enabled on its configured management IP, HTTPS port 8091."
  if $CREATED_MANAGER_KEY; then echo "API key (store privately): $(cat "$MANAGER_CONFIG/api.key")"; fi
  echo "TLS SHA-256 fingerprint:"
  openssl x509 -in "$MANAGER_CONFIG/tls.crt" -noout -fingerprint -sha256
fi

echo "Controlled target installed."
echo "Synthetic target IP: 198.18.0.1/32"
echo "HTTP: http://198.18.0.1:8090"
echo "UDP media sink: 198.18.0.1:9000"
