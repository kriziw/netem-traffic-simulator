"""Real routing/binding verification in disposable Linux network namespaces.

Invoked by CI as root. Host management/default routing is never changed.
"""
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import shlex
import tempfile
import threading
import time
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"status": "ok", "service": "netem-traffic-target", "client": self.client_address[0]}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *_args):
            pass
    def udp():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 9000))
        while True:
            _data, peer = sock.recvfrom(256)
            sock.sendto(peer[0].encode(), peer)
    threading.Thread(target=udp, daemon=True).start()
    http.server.ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()


def client(record, fail=False):
    from trafficgen.traffic_paths import bind_http, probe
    import requests
    if fail:
        assert probe(record)["reachable"] is None
        with bind_http(requests.Session(), record["source"]) as session:
            try:
                session.get("http://198.18.0.1:8090/health", timeout=2)
            except requests.RequestException:
                return
        raise AssertionError("Workload fell back to another route after its policy disappeared")
    with bind_http(requests.Session(), record["source"]) as session:
        for _ in range(5):
            response = session.get("http://198.18.0.1:8090/health", timeout=2)
            assert response.json()["client"] == record["source"]
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        udp.bind((record["source"], 0)); udp.settimeout(2)
        udp.sendto(b"bounded-kernel-check", ("198.18.0.1", 9000))
        assert udp.recv(64).decode() == record["source"]
    finally:
        udp.close()
    assert probe(record)["reachable"] is True


def main():
    if os.geteuid() != 0:
        raise SystemExit("This isolated kernel check requires root on the CI runner")
    from trafficgen import host_admin as admin, network, traffic_paths, network_setup
    original_run = network.run
    marker = uuid.uuid4().hex[:8]
    simulator_ns, router_ns = "nps-" + marker, "npr-" + marker
    script = str(Path(__file__).resolve())
    host_routes = original_run(["ip", "-j", "-4", "route", "show", "table", "main"])
    created = []
    process = None
    dhcp_process = None
    try:
        for namespace in (simulator_ns, router_ns):
            original_run(["ip", "netns", "add", namespace]); created.append(namespace)
            original_run(["ip", "-n", namespace, "link", "set", "lo", "up"])
        original_run(["ip", "-n", router_ns, "address", "add", "198.18.0.1/32", "dev", "lo"])
        for index in range(1, 4):
            left, right = "vl" + marker + str(index), "vr" + marker + str(index)
            original_run(["ip", "link", "add", left, "type", "veth", "peer", "name", right])
            original_run(["ip", "link", "set", left, "netns", simulator_ns])
            original_run(["ip", "link", "set", right, "netns", router_ns])
            original_run(["ip", "-n", simulator_ns, "link", "set", left, "name", "eth" + str(index)])
            original_run(["ip", "-n", simulator_ns, "address", "add", f"10.{250+index}.10.10/24", "dev", "eth" + str(index)])
            original_run(["ip", "-n", router_ns, "address", "add", f"10.{250+index}.10.1/24", "dev", right])
            original_run(["ip", "-n", simulator_ns, "link", "set", "eth" + str(index), "up"])
            original_run(["ip", "-n", router_ns, "link", "set", right, "up"])
        process = subprocess.Popen(["ip", "netns", "exec", router_ns, sys.executable, script, "server"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(30):
            result = subprocess.run(["ip", "netns", "exec", simulator_ns, "curl", "--noproxy", "*", "--silent", "--max-time", "1", "http://10.251.10.1:8090/health"], capture_output=True)
            if result.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Isolated fixture did not start")
        def scoped(args, **kwargs):
            return original_run(["ip", "netns", "exec", simulator_ns, *args], **kwargs)
        with tempfile.TemporaryDirectory() as temporary, patch.object(admin, "ADMIN_DIR", Path(temporary)), \
             patch.object(admin, "run", side_effect=scoped), patch.object(network, "run", side_effect=scoped):
            records = []
            for index, mode in enumerate(("appliance", "direct_netem", "direct_baseline"), 1):
                payload = {"id": str(index) * 32, "name": mode, "mode": mode, "wan_id": "wan1" if mode == "direct_netem" else "",
                           "interface": "eth" + str(index), "gateway": f"10.{250+index}.10.1", "source": f"10.{250+index}.10.10", "target": "198.18.0.1"}
                if index == 1:
                    # Exercise the guided task on a down, unaddressed guest NIC.
                    scoped(["ip", "-4", "address", "del", payload["source"] + "/24", "dev", payload["interface"]])
                    scoped(["ip", "link", "set", payload["interface"], "down"])
                    # ARP evidence can be collected before any IP is assigned.
                    scoped(["ip", "link", "set", payload["interface"], "up"])
                    subprocess.run(["ip", "netns", "exec", simulator_ns, sys.executable, script, "arp", payload["interface"], payload["gateway"]], check=True)
                    assert not next(nic for nic in network.interfaces() if nic["interface"] == payload["interface"])["addresses"]
                    scoped(["ip", "link", "set", payload["interface"], "down"])
                    # Real DHCP exchange; the hook ignores offered DNS/default routes.
                    dhcp_process = subprocess.Popen(["ip", "netns", "exec", router_ns, "dnsmasq", "--no-daemon", "--conf-file=/dev/null", "--port=0",
                        "--bind-interfaces", "--interface=vr" + marker + "1", "--dhcp-range=10.251.10.10,10.251.10.10,255.255.255.0,2m",
                        "--dhcp-option=3,10.251.10.1", "--dhcp-option=6,192.0.2.53", "--dhcp-leasefile="+temporary+"/dnsmasq.leases"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    hook = Path(temporary) / "dhcp-hook.sh"
                    hook.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(script) + " dhcp-hook " + shlex.quote(temporary) + ' "$1"\n')
                    hook.chmod(0o755)
                    (Path(temporary) / "dhcp-interfaces.json").write_text(json.dumps({"eth1":True}))
                    scoped(["ip", "link", "set", payload["interface"], "up"])
                    subprocess.run(["ip","netns","exec",simulator_ns,"busybox","udhcpc","-f","-n","-q","-i","eth1","-s",str(hook),"-t","4","-T","2"],check=True,timeout=15)
                    lease = json.loads((Path(temporary) / "dhcp-eth1.json").read_text())
                    assert lease["state"] == "bound" and lease["gateway"] == "10.251.10.1"
                    assert not scoped(["ip","-4","route","show","default"]).strip(), "DHCP installed a default route"
                    records.append(network_setup.configure({"path": payload, "address": payload["source"] + "/24"}, admin)["path"])
                else:
                    records.append(traffic_paths.configure(payload, admin)["path"])
            # A selected full-workload path coexists with independently bound
            # HTTP and UDP on every other interface, even to the same target.
            for selected in records:
                traffic_paths.select(selected["id"], admin)
                for record in records:
                    subprocess.run(["ip", "netns", "exec", simulator_ns, sys.executable, script, "client", json.dumps(record)], check=True)
            scoped(["ip", "-4", "route", "add", "198.18.0.1/32", "via", "10.252.10.1", "dev", "eth2"])
            scoped(["ip", "-4", "route", "del", "198.18.0.1/32", "table", str(records[0]["policy_table"])])
            subprocess.run(["ip", "netns", "exec", simulator_ns, sys.executable, script, "fail", json.dumps(records[0])], check=True)
            subprocess.run(["ip", "netns", "exec", simulator_ns, sys.executable, script, "client", json.dumps(records[2])], check=True)
            traffic_paths.restore(admin)
            for record in records:
                subprocess.run(["ip", "netns", "exec", simulator_ns, sys.executable, script, "client", json.dumps(record)], check=True)
        assert original_run(["ip", "-j", "-4", "route", "show", "table", "main"]) == host_routes
        print("PASS: three independent HTTP/UDP paths, selection, fail-closed policy and reboot restoration; host routes unchanged")
    finally:
        if dhcp_process:
            dhcp_process.terminate()
            try:
                dhcp_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                dhcp_process.kill(); dhcp_process.wait()
        if process:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        for namespace in reversed(created):
            original_run(["ip", "netns", "delete", namespace])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "server":
        server()
    elif len(sys.argv) > 1 and sys.argv[1] in ("client", "fail"):
        client(json.loads(sys.argv[2]), fail=sys.argv[1] == "fail")
    elif len(sys.argv) > 1 and sys.argv[1] == "arp":
        from trafficgen.setup_probe import arp
        result = arp(sys.argv[2], sys.argv[3])
        assert result["reachable"] is True and result["mac"], result
        print("PASS: unaddressed data NIC received actual gateway ARP evidence")
    elif len(sys.argv) > 1 and sys.argv[1] == "dhcp-hook":
        from trafficgen import host_admin, data_dhcp
        host_admin.ADMIN_DIR = Path(sys.argv[2])
        data_dhcp.event(host_admin,sys.argv[3],os.environ)
    else:
        main()
