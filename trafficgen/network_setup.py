"""Guided guest setup. Proxmox bridge labels are operator-confirmed guidance."""
import ipaddress
import re
import secrets

from . import traffic_paths
from .network import validate_interface

FILE = "network-setup.json"
ROLES = (
    ("appliance", "SD-WAN appliance", "appliance", "vmbr3", "eth1", "", "FortiGate LAN"),
    ("wan1", "WAN1 calibration", "direct_netem", "vmbr1", "eth2", "wan1", "Appliance-facing side of NetEm WAN1"),
    ("wan2", "WAN2 calibration", "direct_netem", "vmbr2", "eth3", "wan2", "Appliance-facing side of NetEm WAN2"),
    ("baseline1", "ISP1 clean baseline", "direct_baseline", "vmbr4", "eth4", "", "Router-facing ISP1 transit"),
    ("baseline2", "ISP2 clean baseline", "direct_baseline", "vmbr5", "eth5", "", "Router-facing ISP2 transit (optional)"),
)


def cards(saved, inventory, paths, management="eth0"):
    result = []
    for role, title, mode, bridge, suggested, wan, connection in ROLES:
        draft = saved.get(role, {})
        nic = next((row for row in inventory if row["interface"] == draft.get("interface", suggested)), None)
        row = dict(role=role, title=title, mode=mode, bridge=bridge, interface=nic["interface"] if nic else "",
                   address=nic["addresses"][0] if nic and len(nic["addresses"]) == 1 else "",
                   name=title, gateway="", target="198.18.0.1", wan_id=wan, connection=connection, suggested=suggested)
        row.update(draft)
        if not draft:
            matches = [path for path in paths if path.get("mode") == mode and path.get("interface") == row["interface"]
                       and path.get("wan_id", "") == wan]
            if len(matches) == 1:
                existing = matches[0]
                row.update({key: existing[key] for key in ("id", "name", "interface", "gateway", "target", "address") if key in existing})
        row["nic"] = next((nic for nic in inventory if nic["interface"] == row["interface"]), None)
        row["path"] = next((path for path in paths if path["id"] == row.get("id")), None)
        row["address_ready"] = bool(row["nic"] and row["nic"].get("up") and row["address"] in row["nic"]["addresses"])
        result.append(row)
    return result


def definition(raw, previous=None, management="eth0"):
    role = next((row for row in ROLES if row[0] == raw.get("role")), None)
    if not role:
        raise ValueError("Choose a setup role.")
    bridge = str(raw.get("bridge", "")).strip()
    interface = str(raw.get("interface", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,40}", bridge):
        raise ValueError("Enter the Proxmox bridge name used for this NIC.")
    if interface in (management, "lo") or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface):
        raise ValueError("Choose an existing data interface, separate from management.")
    try:
        address = ipaddress.IPv4Interface(str(raw.get("address", "")))
        if "/" not in str(raw.get("address", "")):
            raise ValueError("Enter the simulator IPv4 address with its prefix.")
    except ValueError as exc:
        raise ValueError("Enter the simulator IPv4 address with its prefix, for example 10.251.10.10/24.") from exc
    old = previous or {}
    # Changed wiring gets a new policy; existing immutable policies remain available.
    unchanged = all(old.get(key) == value for key, value in
                    (("interface", interface), ("address", str(address)), ("target", str(raw.get("target", "")))))
    path = dict(id=old.get("id") if unchanged and old.get("id") else secrets.token_hex(16),
                name=str(raw.get("name", "")).strip(), mode=role[2], interface=interface,
                source=str(address.ip), gateway=str(raw.get("gateway", "")).strip(),
                target=str(raw.get("target", "")).strip(), wan_id=str(raw.get("wan_id", "")).strip() if role[2] == "direct_netem" else "")
    traffic_paths.validate(path, management, [{"interface": interface, "addresses": [str(address)], "up": True}])
    return dict(path, role=role[0], bridge=bridge, address=str(address))


def payload(row):
    return {key: row[key] for key in ("id", "name", "mode", "interface", "source", "gateway", "target", "wan_id")}


def configure(raw, admin):
    if set(raw) != {"path", "address"} or not isinstance(raw["path"], dict):
        raise ValueError("Invalid network setup task.")
    path = raw["path"]
    address = validate_interface({"interface": path.get("interface"), "address": raw["address"]}, admin.management())
    # Validate the prospective route before any interface mutation.
    traffic_paths.validate(path, admin.management(), [{"interface": address["interface"], "addresses": [address["address"]], "up": True}])
    admin.configure_interface(address)
    try:
        result = traffic_paths.configure(path, admin)
    except (ValueError, OSError) as exc:
        raise ValueError("Interface address saved, but path verification failed. Previous workload selection retained. " + str(exc)) from exc
    result["message"] = "Interface address saved and traffic path verified. Select it for the full workload when ready."
    return result
