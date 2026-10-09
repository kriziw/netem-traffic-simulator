from __future__ import annotations

import hashlib
import json
import socket
import ssl
import threading

from . import __version__

DISCOVERY_MAGIC = "NETEM_TRAFFIC_SIMULATOR_DISCOVERY_V1"


def management_addresses():
    addresses = set()
    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = item[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("1.1.1.1", 53))
        address = sock.getsockname()[0]
        if address and not address.startswith("127."):
            addresses.add(address)
        sock.close()
    except OSError:
        pass
    return sorted(addresses)


def certificate_fingerprint(path):
    try:
        pem = path.read_text()
        der = ssl.PEM_cert_to_DER_cert(pem)
        return hashlib.sha256(der).hexdigest()
    except (OSError, ValueError):
        return None


def discovery_payload(settings):
    return {
        "service": "netem-traffic-simulator",
        "protocol": DISCOVERY_MAGIC,
        "api_version": "v1",
        "version": __version__,
        "instance_name": settings.instance_name,
        "hostname": socket.gethostname(),
        "scheme": "https",
        "api_port": settings.api_port,
        "tls_sha256": certificate_fingerprint(settings.tls_cert),
        "management_addresses": management_addresses(),
        "capabilities": [
            "corporate-workloads",
            "dem",
            "api-key-auth",
            "api-key-rotation",
            "locust-engine",
        ],
    }


def discovery_server(settings, stop_event: threading.Event):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", settings.discovery_port))
    sock.settimeout(1.0)
    payload = discovery_payload(settings)

    while not stop_event.is_set():
        try:
            data, peer = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            request = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if request.get("protocol") != DISCOVERY_MAGIC:
            continue
        response = dict(payload)
        response["nonce"] = request.get("nonce")
        try:
            sock.sendto(json.dumps(response).encode("utf-8"), peer)
        except OSError:
            pass
    sock.close()
