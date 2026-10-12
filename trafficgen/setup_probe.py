"""Bounded, interface-local evidence for setup; no address or route assignment."""
import ipaddress
import re
import subprocess
import time

from . import appliance_identity

FILE = "setup-probes.json"
HINTS = {"appliance": ["appliance"], "isp1": ["wan1", "baseline1"], "isp2": ["wan2", "baseline2"]}


def validate(raw, links, management="eth0"):
    if not isinstance(raw, dict) or set(raw) != {"gateways"} or not isinstance(raw["gateways"], list) or len(raw["gateways"]) > 6:
        raise ValueError("Provide up to six known gateway hints.")
    management_networks = [ipaddress.IPv4Interface(f"{a['local']}/{a['prefixlen']}").network
                           for link in links if link.get("ifname", "").split("@")[0] == management
                           for a in link.get("addr_info", []) if a.get("family") == "inet"]
    result = []
    for hint in raw["gateways"]:
        if not isinstance(hint, dict) or set(hint) != {"gateway", "kind"} or hint["kind"] not in HINTS:
            raise ValueError("Choose appliance, ISP1 or ISP2 gateway hints.")
        try:
            address = ipaddress.IPv4Address(hint["gateway"])
        except ValueError as exc:
            raise ValueError("Gateway hints must be IPv4 addresses.") from exc
        if (address.is_multicast or address.is_unspecified or address.is_loopback or address.is_link_local or address.is_reserved
                or address in ipaddress.IPv4Network("198.18.0.0/15") or any(address in net for net in management_networks)):
            raise ValueError("Probe only data gateways outside management and benchmark networks.")
        row = {"gateway": str(address), "kind": hint["kind"]}
        if row not in result:
            result.append(row)
    return result


def arp(interface, gateway):
    """iputils DAD uses source 0.0.0.0: works before configuring a guest IP."""
    try:
        result = subprocess.run(["arping", "-D", "-c", "1", "-w", "1", "-I", interface, gateway],
                                capture_output=True, text=True, timeout=2, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
        mac = re.search(r"reply from[^\n]*\[([0-9a-f:]{17})\]", result.stdout, re.I)
        if result.returncode == 1 and mac:
            return {"reachable": True, "mac": mac.group(1).lower(), "message": "Fresh ARP reply on this interface."}
        if result.returncode == 0:
            return {"reachable": False, "message": "No ARP reply within the bounded probe; purpose remains unknown."}
        return {"reachable": None, "message": "ARP probe unavailable or failed; no reachability conclusion."}
    except (OSError, subprocess.SubprocessError):
        return {"reachable": None, "message": "ARP probing unavailable. Install the current simulator release to supply iputils-arping."}


def recommendations(observations):
    roles = sorted({role for row in observations if row.get("reachable") is True for role in HINTS[row["kind"]]})
    if roles == ["appliance"]:
        return {"roles": roles, "state": "suggested", "message": "Appliance LAN candidate: the known appliance gateway answered. Confirm its LAN-side bridge."}
    if roles:
        return {"roles": roles, "state": "ambiguous", "message": "These roles share reachable gateways. ARP cannot distinguish the appliance-facing side of NetEm from the router-facing baseline; confirm the Proxmox bridge."}
    return {"roles": [], "state": "unknown", "message": "Insufficient evidence. Supply known data gateway hints or confirm the Proxmox wiring manually."}


def collect(raw, admin):
    links = admin.address_table()
    hints = validate(raw, links, admin.management())
    inventory = admin.interfaces(admin.management())
    nics = inventory[:6]
    lldp = appliance_identity.lldp_signals()
    try:
        neighbors = admin.ip_json("neigh", "show")
    except (ValueError, OSError):
        neighbors = []
    rows = []
    identity_budget = 2
    for nic in nics:
        observations = []
        was_up = nic["up"]
        try:
            if hints and not was_up:
                admin.run(["ip", "link", "set", "dev", nic["interface"], "up"])
            for hint in hints:
                result = arp(nic["interface"], hint["gateway"])
                result.update(hint)
                observations.append(result)
        except (ValueError, OSError):
            observations.append({"reachable": None, "message": "Cannot enable this guest link for probing."})
        finally:
            if hints and not was_up:
                # Restoration failure must fail the task, not claim no mutation.
                admin.run(["ip", "link", "set", "dev", nic["interface"], "down"])
        row = dict(interface=nic["interface"], checked_at=time.time(), addresses=nic["addresses"],
                   observations=observations, recommendation=recommendations(observations), identities=[])
        # Neighbor identity helps explain the evidence, but is never a role proof.
        candidates = [{"interface": nic["interface"], "gateway": item["gateway"], "mac": item["mac"]}
                      for item in observations if item.get("reachable") is True and item.get("mac")]
        for candidate in candidates:
            signals = [(source, text) for source, text in lldp.get(nic["interface"], []) if candidate["mac"] in text.lower()]
            if was_up and nic["addresses"] and identity_budget:
                identity_budget -= 1
                signals.extend(appliance_identity.web_signals(candidate, inventory))
            identity = appliance_identity.classify(signals)
            row["identities"].append({"gateway": candidate["gateway"], "vendor": identity["vendor"], "confidence": identity["confidence"]})
        row["cached_neighbors"] = [{"ip": neighbor.get("dst"), "mac": neighbor.get("lladdr")}
                                   for neighbor in neighbors if neighbor.get("dev") == nic["interface"] and neighbor.get("lladdr")]
        row["lldp_present"] = bool(lldp.get(nic["interface"]))
        rows.append(row)
    report = {"checked_at": time.time(), "interfaces": rows, "gateways": hints,
              "message": "Bounded probes completed. No addresses/routes configured; temporarily enabled links restored.",
              "limited": len(inventory) > len(nics)}
    admin.atomic_json(admin.ADMIN_DIR / FILE, report)
    return {"message": report["message"], "report": report}
