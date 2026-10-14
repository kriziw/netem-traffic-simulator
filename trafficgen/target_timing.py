"""A small, independent UDP echo to the workload's controlled target.

One probe per second per run, never per user. It provides network-path round
trip observations, never application transactions or independent user samples.
"""
import socket
import time
import uuid
from urllib.parse import urlsplit

from .wire import ADDRESS_REQUEST


def probe(config):
    target = urlsplit(config['target']).hostname
    family, kind, protocol, _, address = socket.getaddrinfo(target, config['udp_port'], type=socket.SOCK_DGRAM)[0]
    sock = socket.socket(family, kind, protocol)
    header = uuid.uuid4().bytes[:12]
    started_at = time.time()
    result = {'target_address': address[0], 'target_port': config['udp_port'], 'started_at': started_at,
              'source': (config.get('traffic_path') or {}).get('source'), 'rtd_ms': None, 'success': False,
              'protocol': 'UDP controlled-target echo', 'request_bytes': 16}
    try:
        if result['source']:
            sock.bind((result['source'], 0))
        sock.connect(address)
        sock.settimeout(2)
        origin = time.monotonic()
        sock.send(header + ADDRESS_REQUEST)
        while time.monotonic() - origin < 2:
            sock.settimeout(max(.001, 2 - (time.monotonic() - origin)))
            data = sock.recv(128)
            received = time.monotonic()
            if data[:12] == header:
                result.update(success=True, rtd_ms=(received-origin)*1000, response_bytes=len(data))
                break
        if not result['success']:
            result['error'] = 'No matching target echo'
    except OSError as exc:
        result['error'] = type(exc).__name__
    finally:
        sock.close()
    return result
