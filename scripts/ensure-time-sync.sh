#!/usr/bin/env bash
# Keep this host on a time server: NetEm, the simulator and the target compare timestamps.
# A container shares its host's clock, so it is left alone; keep the Proxmox host on NTP.
set -uo pipefail
virt="$(systemd-detect-virt --container 2>/dev/null || true)"
if [[ -n "$virt" && "$virt" != "none" ]]; then
  echo "Container ($virt): the clock comes from the host. Keep the host on NTP."
  exit 0
fi
if [[ "$(timedatectl show --property=NTP --value 2>/dev/null)" == "yes" ]]; then
  exit 0
fi
if ! systemctl list-unit-files 2>/dev/null | grep -Eq '^(chrony|ntp|ntpsec|systemd-timesyncd)\.service'; then
  apt-get install -y systemd-timesyncd || true
fi
timedatectl set-ntp true || echo "Could not turn on time sync. Run: timedatectl set-ntp true" >&2
exit 0
