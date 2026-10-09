"""Vendor-neutral LAN candidates and constrained benchmark route validation."""
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import re
import subprocess

BENCHMARK = ipaddress.ip_network('198.18.0.0/15')
VENDORS = ('Fortinet', 'VeloCloud', 'Cisco', 'Other')


def run(args, timeout=8):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'Network command failed.')
    return result.stdout


def ip_json(*args):
    return json.loads(run(['ip', '-j', '-4', *args]))


def interfaces(management='eth0'):
    rows = []
    for link in ip_json('address', 'show'):
        name = link.get('ifname', '').split('@')[0]
        if name in ('lo', management) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', name):
            continue
        addresses = [f"{a['local']}/{a['prefixlen']}" for a in link.get('addr_info', [])
                     if a.get('family') == 'inet' and a.get('scope') == 'global']
        if addresses:
            rows.append({'interface': name, 'addresses': addresses, 'up': 'UP' in link.get('flags', [])})
    return rows


def validate_route(payload, management='eth0', inventory=None):
    if not isinstance(payload, dict):
        raise ValueError('Route must be an object.')
    interface = str(payload.get('interface', ''))
    if interface == management or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', interface):
        raise ValueError('Choose a data interface; management routing cannot be changed.')
    try:
        gateway = ipaddress.IPv4Address(payload.get('gateway', ''))
        target = ipaddress.IPv4Address(payload.get('target', '198.18.0.1'))
    except ipaddress.AddressValueError as exc:
        raise ValueError('Gateway and target must be IPv4 addresses.') from exc
    if target not in BENCHMARK or target.is_multicast or gateway.is_multicast or gateway.is_unspecified:
        raise ValueError('Target must be in 198.18.0.0/15; gateway must be unicast.')
    found = next((item for item in (inventory if inventory is not None else interfaces(management))
                  if item['interface'] == interface and item.get('up', True)), None)
    if not found:
        raise ValueError('Data interface is missing, down, or has no IPv4 address.')
    addresses = [ipaddress.ip_interface(a) for a in found['addresses']]
    matching = [a for a in addresses if gateway in a.network and gateway != a.ip
                and (a.network.prefixlen >= 31 or gateway not in (a.network.network_address, a.network.broadcast_address))]
    if not matching or any(target in a.network for a in addresses):
        raise ValueError('Gateway must be on the selected LAN and target must be upstream, outside that LAN.')
    return {'interface': interface, 'gateway': str(gateway), 'target': str(target), 'source': str(matching[0].ip)}


def discover(management='eth0', scan=False):
    inventory = interfaces(management)
    if scan:
        # Active discovery is explicit, limited to directly connected data LANs <= /24.
        hosts = []
        for row in inventory:
            for address in row['addresses']:
                a = ipaddress.ip_interface(address)
                if a.network.prefixlen < 24:
                    continue
                hosts.extend((row['interface'], str(ip)) for ip in a.network.hosts() if ip != a.ip)
        if len(hosts) > 1024:
            raise ValueError('Scan exceeds 1024 LAN addresses; use neighbor discovery or manual entry.')
        def probe(pair):
            try:
                subprocess.run(['ping', '-n', '-I', pair[0], '-c', '1', '-W', '1', pair[1]],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(probe, hosts))
    allowed = {row['interface'] for row in inventory}
    gateways = {(r.get('dev'), r.get('gateway')) for r in ip_json('route', 'show') if r.get('gateway')}
    candidates = {}
    for row in ip_json('neigh', 'show'):
        if row.get('dev') not in allowed or not row.get('lladdr') or 'FAILED' in row.get('state', []):
            continue
        key = (row['dev'], row['dst'])
        candidates[key] = {'interface': row['dev'], 'gateway': row['dst'], 'mac': row['lladdr'],
                           'evidence': 'configured gateway' if key in gateways else 'LAN neighbor; gateway role unverified'}
    for interface, gateway in gateways:
        if interface in allowed:
            candidates.setdefault((interface, gateway), {'interface': interface, 'gateway': gateway,
                'mac': None, 'evidence': 'configured gateway'})
    return {'interfaces': inventory, 'candidates': list(candidates.values()),
            'note': 'Candidates are not confirmed SD-WAN devices. Verify the LAN gateway and assign a vendor label.'}
