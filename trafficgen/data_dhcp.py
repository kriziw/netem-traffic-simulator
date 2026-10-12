"""DHCP on explicitly opted-in data NICs, with no default-route or DNS hooks."""
import ipaddress
import os
import re
import sys
import time

from .maintenance import read_json

FILE = "dhcp-interfaces.json"


def enabled(admin, interface):
    return bool(read_json(admin.ADMIN_DIR / FILE, {}).get(interface))


def lease(admin, interface):
    row = read_json(admin.ADMIN_DIR / ("dhcp-" + interface + ".json"), {})
    if row.get("state") == "bound" and row.get("expires_at", 0) <= time.time():
        return dict(row, state="expired", message="DHCP lease expired. Request an address again.")
    return row


def validate_nic(admin, interface):
    if (interface in ("lo", admin.management()) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", str(interface))
            or not any(row["interface"] == interface for row in admin.interfaces(admin.management()))):
        raise ValueError("Choose an existing data interface; management DHCP cannot be changed.")
    return interface


def _remove_owned(admin, interface, row):
    if row.get("address") and any(row["address"] in nic["addresses"] for nic in admin.interfaces(admin.management()) if nic["interface"] == interface):
        admin.run(["ip", "-4", "address", "del", row["address"], "dev", interface])


def event(admin, action, values):
    interface = validate_nic(admin, values.get("interface", ""))
    if not enabled(admin, interface):
        raise ValueError("DHCP is not enabled for this data interface.")
    old = read_json(admin.ADMIN_DIR / ("dhcp-" + interface + ".json"), {})
    path = admin.ADMIN_DIR / ("dhcp-" + interface + ".json")
    if action == "deconfig":
        _remove_owned(admin, interface, old)
        admin.atomic_json(path, {"state":"waiting", "checked_at":time.time(), "message":"Waiting for a DHCP address from this network."})
        return
    if action not in ("bound", "renew"):
        return
    try:
        address = ipaddress.IPv4Interface(values["ip"] + "/" + values["subnet"])
        seconds = int(values["lease"])
        if not 1 <= seconds <= 4294967295:
            raise ValueError("Invalid DHCP lease duration.")
        links = admin.address_table()
        # A lease may replace only an address previously owned by this DHCP client.
        filtered = []
        for link in links:
            copy = dict(link)
            if link.get("ifname", "").split("@")[0] == interface:
                copy["addr_info"] = [a for a in link.get("addr_info", []) if not
                    (a.get("family") == "inet" and f"{a['local']}/{a['prefixlen']}" == old.get("address"))]
            filtered.append(copy)
        from .network import validate_interface
        validate_interface({"interface":interface,"address":str(address)}, admin.management(), filtered)
        routers = str(values.get("router", "")).split()
        gateway = str(ipaddress.IPv4Address(routers[0])) if routers else ""
        if gateway:
            from .network import validate_route
            validate_route({"interface":interface,"source":str(address.ip),"gateway":gateway,"target":"198.18.0.1"},
                           admin.management(), [{"interface":interface,"addresses":[str(address)],"up":True}])
        row = dict(state="bound", interface=interface, address=str(address), source=str(address.ip), gateway=gateway,
                   checked_at=time.time(), expires_at=time.time()+seconds,
                   message="Address received from DHCP." if gateway else "Address received; DHCP supplied no router gateway.")
        admin.run(["ip", "-4", "address", "replace", str(address), "dev", interface])
        if old.get("address") != str(address):
            _remove_owned(admin, interface, old)
        admin.atomic_json(path, row)
    except (ValueError, KeyError) as exc:
        admin.atomic_json(path, dict(old, state="error",checked_at=time.time(),message="DHCP reply rejected: " + str(exc)))
        raise


def configure(raw, admin, wait=12):
    if set(raw) != {"interface"}:
        raise ValueError("Choose one data interface for DHCP.")
    interface = validate_nic(admin, raw["interface"])
    config = read_json(admin.ADMIN_DIR / FILE, {})
    nic = next(row for row in admin.interfaces(admin.management()) if row["interface"] == interface)
    if not config.get(interface):
        if nic["addresses"] or interface in read_json(admin.ADMIN_DIR / "interfaces.json", {}):
            raise ValueError("This interface already has an address. Keep its existing settings; do not start a second DHCP client.")
        if len(config) >= 6:
            raise ValueError("Up to six data interfaces can use app-managed DHCP.")
        # Existing Proxmox/ifupdown DHCP should remain under that manager.
        from pathlib import Path
        files = [Path("/etc/network/interfaces"), *Path("/etc/network/interfaces.d").glob("*")]
        for file in files:
            if file.is_file() and re.search(r"^\s*iface\s+" + re.escape(interface) + r"\s+inet\s+dhcp\b", file.read_text(errors="replace"), re.M):
                raise ValueError("DHCP is already configured by Proxmox/ifupdown on this NIC. Bring it up there, then refresh.")
    config[interface] = True
    admin.atomic_json(admin.ADMIN_DIR / FILE, config)
    admin.run(["ip", "link", "set", "dev", interface, "up"])
    unit = "netem-traffic-simulator-dhcp@" + interface + ".service"
    admin.run(["systemctl", "enable", "--now", unit])
    if enabled(admin, interface) and nic['addresses']:
        admin.run(["systemctl", "kill", "--kill-who=main", "--signal=USR1", unit])
    until = time.monotonic() + wait
    while time.monotonic() < until:
        current = lease(admin, interface)
        if current.get("state") in ("bound", "error"):
            return {"lease":current,"message":current["message"]}
        time.sleep(.25)
    return {"lease":lease(admin, interface),"message":"Automatic addressing enabled. Waiting for the router; check the bridge and DHCP server if no address arrives."}


def stop(raw, admin):
    if set(raw) != {"interface"}:
        raise ValueError("Choose one DHCP interface.")
    interface = validate_nic(admin, raw["interface"])
    if not enabled(admin, interface):
        raise ValueError("This interface is not managed by the app's DHCP client.")
    paths = read_json(admin.ADMIN_DIR / "traffic-paths.json", [])
    if any(path.get("interface") == interface for path in paths):
        raise ValueError("Remove traffic paths on this interface before stopping DHCP.")
    admin.run(["systemctl", "disable", "--now", "netem-traffic-simulator-dhcp@" + interface + ".service"])
    _remove_owned(admin, interface, read_json(admin.ADMIN_DIR / ("dhcp-" + interface + ".json"), {}))
    config = read_json(admin.ADMIN_DIR / FILE, {})
    config.pop(interface, None)
    admin.atomic_json(admin.ADMIN_DIR / FILE, config)
    admin.atomic_json(admin.ADMIN_DIR / ("dhcp-" + interface + ".json"), {})
    return {"message":"Automatic addressing stopped; the app-owned address was removed."}


def main():
    from . import host_admin as admin
    if os.geteuid() != 0:
        raise SystemExit("Root DHCP helper required.")
    action = sys.argv[1]
    if action == "client":
        interface = validate_nic(admin, sys.argv[2])
        if not enabled(admin, interface):
            raise SystemExit("DHCP is not enabled for this interface.")
        admin.run(["ip", "link", "set", "dev", interface, "up"])
        os.execv("/usr/bin/busybox", ["busybox","udhcpc","-f","-R","-i",interface,"-s",str(admin.APP_DIR / "scripts/data-dhcp-hook.sh"),
                 "-p","/run/netem-dhcp."+interface+".pid","-t","3","-T","3","-A","30"])
    else:
        import fcntl
        with (admin.ADMIN_DIR / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            event(admin, action, os.environ)
            if action in ("bound", "renew"):
                current = lease(admin, os.environ["interface"])
                from . import traffic_paths
                selected = read_json(admin.ADMIN_DIR / "route.json", {}) or {}
                for path in read_json(admin.ADMIN_DIR / traffic_paths.FILE, []):
                    if path.get("interface") == os.environ["interface"] and path.get("source") == current.get("source"):
                        payload = {key:path[key] for key in ("id","name","mode","wan_id","interface","source","gateway","target")}
                        if selected.get("id") == path["id"]:
                            payload["select"] = True
                        traffic_paths.configure(payload, admin, verify=False)
