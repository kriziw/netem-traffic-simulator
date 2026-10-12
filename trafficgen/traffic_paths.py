"""Source-bound workload and probe paths; never change management/default routes."""
from __future__ import annotations

import http.client
import ipaddress
import json
import re
import time

import requests
from requests.adapters import HTTPAdapter

from .maintenance import read_json
from .network import validate_route, ip_json, interfaces

MODES = {"appliance", "direct_netem", "direct_baseline"}
MAX_PATHS = 8
TABLE_BASE = 21000
PRIORITY_BASE = 22000
FILE = "traffic-paths.json"


def validate(payload, management="eth0", inventory=None):
    allowed = {"id", "name", "mode", "wan_id", "interface", "gateway", "target", "source", "select"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("Provide a named traffic path with a data interface, source and router gateway.")
    path_id = str(payload.get("id") or "")
    mode = payload.get("mode")
    name = str(payload.get("name") or "").strip()
    wan_id = str(payload.get("wan_id") or "")
    if not re.fullmatch(r"[a-f0-9]{16,32}", path_id) or mode not in MODES or not 1 <= len(name) <= 80:
        raise ValueError("Invalid traffic path ID, name or mode.")
    if mode == "direct_netem" and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", wan_id):
        raise ValueError("A direct NetEm path must identify its WAN.")
    if mode != "direct_netem" and wan_id:
        raise ValueError("Only a direct NetEm path can specify a WAN.")
    inventory = interfaces(management) if inventory is None else inventory
    route = validate_route(payload, management, inventory)
    if not payload.get("source"):
        nic = next(row for row in inventory if row["interface"] == route["interface"])
        addresses = [ipaddress.IPv4Interface(address) for address in nic["addresses"]]
        if sum(ipaddress.IPv4Address(route["gateway"]) in address.network for address in addresses) > 1:
            raise ValueError("Choose an explicit source address when a data NIC has multiple matching IPv4 addresses.")
    return dict(route, id=path_id, name=name, mode=mode, wan_id=wan_id)


def metadata(route):
    return {key: route.get(key) for key in ("id", "name", "mode", "wan_id", "interface", "source", "gateway", "target", "policy_table")}


class SourceAdapter(HTTPAdapter):
    def __init__(self, source, **kwargs):
        self.source = str(ipaddress.IPv4Address(source))
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        super().init_poolmanager(connections, maxsize, block, source_address=(self.source, 0), **kwargs)


def bind_http(session, source):
    session.trust_env = False
    session.mount("http://", SourceAdapter(source))
    session.mount("https://", SourceAdapter(source))
    return session


def policy_status(route, inventory=None, management="eth0"):
    try:
        from . import data_dhcp, maintenance
        if data_dhcp.enabled(maintenance, route['interface']):
            current = data_dhcp.lease(maintenance, route['interface'])
            if current.get('state') != 'bound' or current.get('source') != route['source']:
                return {"active":False,"state":"error","message":"DHCP lease expired or changed; verify this path with its current address."}
        validate_route(route, management, inventory)
        actual = ip_json("route", "get", route["target"], "from", route["source"])[0]
        active = (actual.get("dev") == route["interface"] and actual.get("gateway") == route["gateway"]
                  and str(actual.get("table")) == str(route["policy_table"]) and "linkdown" not in actual.get("flags", []))
        rules = ip_json("rule", "show")
        active = active and all(any(rule.get("priority") == route["policy_priority"] + int(terminal)
                                   and rule.get("src") in (route["source"], route["source"] + "/32")
                                   and rule.get("dst") in (route["target"], route["target"] + "/32")
                                   and (rule.get("action") == "unreachable" if terminal else str(rule.get("table")) == str(route["policy_table"]))
                                   for rule in rules) for terminal in (False, True))
        return {"active": active, "state": "active" if active else "error",
                "message": "Source-bound path active." if active else "The source-bound routing policy is missing or changed."}
    except (OSError, ValueError, KeyError, IndexError):
        return {"active": False, "state": "error", "message": "Cannot verify the source-bound data path."}


def probe(route, management="eth0"):
    result = dict(metadata(route), checked_at=time.time(), reachable=None, latency_ms=None)
    if not policy_status(route, management=management)["active"]:
        return dict(result, message="Path policy is inactive; no fallback probe was sent.")
    connection = http.client.HTTPConnection(route["target"], 8090, timeout=3, source_address=(route["source"], 0))
    started = time.monotonic()
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        data = response.read(16385)
        if response.status != 200 or len(data) > 16384:
            raise ValueError("Invalid controlled-target reply")
        body = json.loads(data)
        if not isinstance(body, dict) or body.get("service") != "netem-traffic-target" or body.get("status") != "ok":
            raise ValueError("Not the controlled target")
        return dict(result, reachable=True, latency_ms=round((time.monotonic() - started) * 1000, 2), message="Controlled target answered on this path.")
    except (OSError, ValueError, http.client.HTTPException):
        return dict(result, reachable=False, message="Controlled target did not answer on this path; device power state is unknown.")
    finally:
        connection.close()


def _table_routes(admin, table):
    try:
        return admin.ip_json("route", "show", "table", str(table))
    except ValueError as exc:
        if "FIB table does not exist" in str(exc):
            return []
        raise


def _rule_args(record, terminal=False):
    args = ["pref", str(record["policy_priority"] + int(terminal)), "from", record["source"] + "/32", "to", record["target"] + "/32"]
    return args + (["unreachable"] if terminal else ["lookup", str(record["policy_table"])])


def _route_args(record):
    return [record["target"] + "/32", "via", record["gateway"], "dev", record["interface"],
            "onlink", "src", record["source"], "proto", "186", "table", str(record["policy_table"])]


def _check_ownership(admin, record, previous):
    routes = _table_routes(admin, record["policy_table"])
    if routes and (not previous or any(not admin.managed(route) or route.get("dst") not in (previous["target"], previous["target"] + "/32")
                                      or route.get("gateway") != previous["gateway"] or route.get("dev") != previous["interface"] for route in routes)):
        raise ValueError("The reserved policy table contains an unmanaged or changed route.")
    rules = admin.ip_json("rule", "show")
    for terminal in (False, True):
        priority = record["policy_priority"] + int(terminal)
        occupied = [rule for rule in rules if rule.get("priority") == priority]
        if occupied and (not previous or len(occupied) != 1):
            raise ValueError("The reserved policy-rule priority is already in use.")
        for rule in occupied:
            if (rule.get("src") not in (previous["source"], previous["source"] + "/32")
                    or rule.get("dst") not in (previous["target"], previous["target"] + "/32")
                    or (rule.get("action") != "unreachable" if terminal else str(rule.get("table")) != str(previous["policy_table"]))):
                raise ValueError("The reserved policy rule was changed outside the simulator.")
    return rules


def _install(admin, record, rules):
    # A terminal rule prevents falling through to an appliance/default route if
    # this policy table disappears. Only this source and benchmark target match.
    for terminal in (True, False):
        if not any(rule.get("priority") == record["policy_priority"] + int(terminal) for rule in rules):
            admin.run(["ip", "-4", "rule", "add", *_rule_args(record, terminal)])
    admin.run(["ip", "-4", "route", "replace", *_route_args(record)])


def configure(payload, admin, verify=True):
    record = validate(payload, admin.management())
    paths = read_json(admin.ADMIN_DIR / FILE, [])
    previous = next((path for path in paths if path["id"] == record["id"]), None)
    if not previous and len(paths) >= MAX_PATHS:
        raise ValueError("Up to eight traffic paths can be configured.")
    if any(path["id"] != record["id"] and path["source"] == record["source"] for path in paths):
        raise ValueError("Each independent path needs a distinct source IPv4 address.")
    legacy = read_json(admin.ADMIN_DIR / "route.json", {}) or {}
    if (not legacy.get("policy_table") and legacy.get("source") == record["source"] and record["mode"] != "appliance"):
        raise ValueError("This source belongs to the legacy appliance route. Use a distinct address/interface for a direct path.")
    if previous and any(previous[key] != record[key] for key in ("source", "target", "interface")):
        raise ValueError("A saved policy's source, target and interface are immutable. Add a new path for new wiring.")
    table = previous["policy_table"] if previous else next(table for table in range(TABLE_BASE, TABLE_BASE + MAX_PATHS)
                                                            if table not in {path["policy_table"] for path in paths})
    record.update(policy_table=table, policy_priority=PRIORITY_BASE + 2 * (table - TABLE_BASE))
    rules = _check_ownership(admin, record, previous)
    try:
        _install(admin, record, rules)
        actual = admin.ip_json("route", "get", record["target"], "from", record["source"])[0]
        if (actual.get("dev") != record["interface"] or actual.get("gateway") != record["gateway"]
                or str(actual.get("table")) != str(table)):
            raise ValueError("Kernel route does not match this source-bound policy.")
        if verify:
            raw = admin.run(["curl", "--noproxy", "*", "--interface", record["source"], "--fail", "--silent", "--show-error",
                             "--connect-timeout", "3", "--max-time", "6", f"http://{record['target']}:8090/health"], timeout=8)
            body = json.loads(raw)
            if not isinstance(body, dict) or body.get("service") != "netem-traffic-target" or body.get("status") != "ok":
                raise ValueError("The policy path did not reach the controlled target.")
        admin.atomic_json(admin.ADMIN_DIR / FILE, [path for path in paths if path["id"] != record["id"]] + [record])
        if payload.get("select") is True:
            admin.atomic_json(admin.ADMIN_DIR / "route.json", record)
        return {"path": record, "message": "Traffic path verified and saved." if verify else "Traffic path restored."}
    except Exception as original:
        try:
            if previous:
                _install(admin, previous, admin.ip_json("rule", "show"))
            else:
                current = admin.ip_json("rule", "show")
                for terminal in (False, True):
                    if any(rule.get("priority") == record["policy_priority"] + int(terminal) for rule in current):
                        admin.run(["ip", "-4", "rule", "del", *_rule_args(record, terminal)])
                if _table_routes(admin, table):
                    admin.run(["ip", "-4", "route", "del", record["target"] + "/32", "table", str(table)])
        except Exception as rollback:
            raise ValueError("Policy installation failed and rollback needs inspection. No workload may start until the route is verified.") from rollback
        raise original


def select(path_id, admin, verify=True):
    record = next((path for path in read_json(admin.ADMIN_DIR / FILE, []) if path["id"] == path_id), None)
    if not record:
        raise ValueError("Choose a configured traffic path.")
    payload = {key: record[key] for key in ("id", "name", "mode", "wan_id", "interface", "gateway", "target", "source")}
    return configure(dict(payload, select=True), admin, verify=verify)


def restore(admin):
    for path in read_json(admin.ADMIN_DIR / FILE, []):
        from . import data_dhcp
        if data_dhcp.enabled(admin, path['interface']):
            current = data_dhcp.lease(admin, path['interface'])
            nic = next((nic for nic in admin.interfaces(admin.management()) if nic['interface'] == path['interface']), {})
            if current.get('state') != 'bound' or path['address'] not in nic.get('addresses', []):
                continue
        admin.configure_interface({"interface": path["interface"], "address": path["address"]}, persist=False)
        payload = {key: path[key] for key in ("id", "name", "mode", "wan_id", "interface", "gateway", "target", "source")}
        configure(payload, admin, verify=False)


def remove(path_id, admin):
    paths = read_json(admin.ADMIN_DIR / FILE, [])
    record = next((path for path in paths if path["id"] == path_id), None)
    if not record:
        raise ValueError("Traffic path does not exist.")
    if (read_json(admin.ADMIN_DIR / "route.json", {}) or {}).get("id") == path_id:
        raise ValueError("Select another workload path before removing this one.")
    rules = _check_ownership(admin, record, record)
    for terminal in (False, True):
        if any(rule.get("priority") == record["policy_priority"] + int(terminal) for rule in rules):
            admin.run(["ip", "-4", "rule", "del", *_rule_args(record, terminal)])
    if _table_routes(admin, record["policy_table"]):
        admin.run(["ip", "-4", "route", "del", record["target"] + "/32", "table", str(record["policy_table"])])
    admin.atomic_json(admin.ADMIN_DIR / FILE, [path for path in paths if path["id"] != path_id])
    return {"message": "Unused path policy removed; management/default routes are unchanged."}
