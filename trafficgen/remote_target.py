"""Simulator-side target management: pinned HTTPS on the management LAN only."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import ssl
import time

from .appliance_identity import bound_socket
from .maintenance import read_json, version_tuple
from .network import ip_json

PORT = 8091
SERVICE = 'netem-traffic-target-manager'
SECRET_FILE = 'target-secret.json'
STATUS_FILE = 'target-status.json'


def validate_config(payload, management='eth0', links=None):
    if not isinstance(payload, dict):
        raise ValueError('Target connection must be an object.')
    try:
        host = ipaddress.IPv4Address(str(payload.get('host', '')))
    except ValueError as exc:
        raise ValueError('Provide the modem management IPv4 address.') from exc
    key = str(payload.get('api_key', ''))
    pin = str(payload.get('fingerprint', '')).lower().replace(':', '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,160}', key):
        raise ValueError('Provide the target management API key printed by its installer.')
    if not re.fullmatch('[a-f0-9]{64}', pin):
        raise ValueError('Provide the target certificate SHA-256 fingerprint printed by its installer.')
    links = ip_json('address', 'show') if links is None else links
    addresses = [ipaddress.IPv4Interface(f"{a['local']}/{a['prefixlen']}")
                 for link in links if link.get('ifname', '').split('@')[0] == management and 'UP' in link.get('flags', [])
                 for a in link.get('addr_info', []) if a.get('family') == 'inet' and a.get('scope', 'global') == 'global']
    address = next((a for a in addresses if host in a.network and host != a.ip
                    and not host.is_multicast and not host.is_loopback and not host.is_unspecified
                    and (a.network.prefixlen >= 31 or host not in (a.network.network_address, a.network.broadcast_address))), None)
    if not address:
        raise ValueError('The target must be on the simulator directly connected management IPv4 LAN.')
    return {'host': str(host), 'api_key': key, 'fingerprint': pin,
            'interface': management, 'source': str(address.ip)}


class Client:
    def __init__(self, config):
        self.config = config

    def request(self, method, path, payload=None):
        if (method, path) not in (('GET', '/api/v1/status'), ('POST', '/api/v1/check'), ('POST', '/api/v1/update')):
            raise ValueError('Unsupported target management endpoint.')
        c = self.config
        connection = sock = None
        try:
            sock = bound_socket(c['interface'], c['source'], c['host'], PORT, timeout=8)
            # The independently verified pin authenticates even a self-signed lab certificate.
            sock = ssl._create_unverified_context().wrap_socket(sock, server_hostname=c['host'])
            actual = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
            if not hmac.compare_digest(actual, c['fingerprint']):
                raise ValueError('Target certificate mismatch. Verify its fingerprint before reconnecting.')
            connection = http.client.HTTPConnection(c['host'], PORT, timeout=8)
            connection.sock = sock
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request(method, path, body=body, headers={
                'Authorization': 'Bearer ' + c['api_key'], 'Content-Type': 'application/json', 'Connection': 'close'})
            response = connection.getresponse()
            if response.status not in (200, 202):
                raise ValueError(f'Target management returned HTTP {response.status}. Check its API key and task status.')
            raw = response.read(32769)
            if len(raw) > 32768:
                raise ValueError('Target management response is too large.')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('Invalid target management response.')
            return data
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            raise ValueError('Cannot reach target management. Check its management IP, API service and connectivity.') from exc
        finally:
            if connection:
                connection.close()
            elif sock:
                sock.close()


def public_status(data, config):
    if data.get('service') != SERVICE:
        raise ValueError('The management address did not reach a controlled target manager.')
    version_tuple(str(data.get('version', '')))
    release = data.get('release') or {}
    job = data.get('job') or {}
    if not isinstance(release, dict) or not isinstance(job, dict):
        raise ValueError('Invalid target task status.')
    return {'connected': True, 'host': config['host'], 'fingerprint': config['fingerprint'],
            'version': data['version'], 'ready': bool(data.get('ready')), 'busy': bool(data.get('busy')),
            'release': {key: release.get(key) for key in ('tag', 'display_tag', 'available', 'checked_at')},
            'job': {key: job.get(key) for key in ('id', 'action', 'state', 'message', 'timestamp')},
            'checked_at': time.time()}


def perform(action, payload, admin_dir, management='eth0'):
    from .host_admin import atomic_json

    if action == 'disconnect_target':
        (admin_dir / SECRET_FILE).unlink(missing_ok=True)
        atomic_json(admin_dir / STATUS_FILE, {'connected': False})
        return {'message': 'Target management connection and API key removed.'}
    config = validate_config(payload if action == 'configure_target' else read_json(admin_dir / SECRET_FILE), management)
    client = Client(config)

    def refresh(persist=True):
        state = public_status(client.request('GET', '/api/v1/status'), config)
        if persist:
            atomic_json(admin_dir / STATUS_FILE, state)
        return state

    state = refresh(persist=action != 'configure_target')
    if action == 'configure_target':
        if not state['ready']:
            raise ValueError('Target update worker is not installed. Run the current target installer once.')
        atomic_json(admin_dir / SECRET_FILE, config, mode=0o600)
        atomic_json(admin_dir / STATUS_FILE, state)
        return {'message': 'Target management connected and certificate verified.'}
    if action == 'target_status':
        return {'message': 'Target status refreshed.'}
    if action not in ('target_check', 'target_install'):
        raise ValueError('Unsupported target action.')
    if state['busy']:
        raise ValueError('A target administration task is already running.')
    if action == 'target_install':
        tag = str(payload.get('tag', ''))
        version_tuple(tag)
        if not state['release'].get('available') or state['release'].get('tag') != tag:
            raise ValueError('Check target releases again before installing.')
        request_path, request_payload = '/api/v1/update', {'tag': tag}
    else:
        request_path, request_payload = '/api/v1/check', {}
    reply = client.request('POST', request_path, request_payload)
    job_id = str(reply.get('job_id', ''))
    if not re.fullmatch('[a-f0-9]{32}', job_id):
        raise ValueError('Target did not return a valid update job identity.')
    deadline = time.monotonic() + (1100 if action == 'target_install' else 90)
    last_error = None
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            state = refresh()
        except ValueError as exc:
            # Target API restarts during installation. The next poll reconnects.
            last_error = exc
            continue
        job = state['job']
        if job.get('id') != job_id:
            continue
        if job.get('state') == 'failed':
            raise ValueError(str(job.get('message') or 'Target update failed.')[:500])
        if job.get('state') == 'completed':
            if action == 'target_install' and version_tuple(state['version']) != version_tuple(tag):
                raise ValueError('Target task completed but its running version does not match the requested release.')
            return {'message': 'Target update installed and verified.' if action == 'target_install' else 'Target release check completed.'}
    raise ValueError('Target task has not completed. Refresh target status before trying again.' +
                     (' ' + str(last_error) if last_error else ''))
