#!/usr/bin/env bash
set -euo pipefail
# Proxmox unprivileged LXCs may not permit systemd's mount namespaces.
# Keep the service account, filesystem permissions and NoNewPrivileges.
SERVICE_NAME="$1"
DROPIN_DIR="/etc/systemd/system/${SERVICE_NAME}.service.d"
if [[ "$(systemd-detect-virt --container 2>/dev/null || true)" == "lxc" ]]; then
  mkdir -p "$DROPIN_DIR"
  cat > "$DROPIN_DIR/10-lxc.conf" <<'EOF'
[Service]
PrivateTmp=false
ProtectSystem=false
ProtectHome=false
ReadWritePaths=
ProtectKernelTunables=false
ProtectKernelModules=false
ProtectControlGroups=false
EOF
else
  rm -f "$DROPIN_DIR/10-lxc.conf"
fi
