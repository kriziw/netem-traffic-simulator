"""Best-effort product hints. Observed identity is never proof of a usable SD-WAN gateway."""
from __future__ import annotations
import concurrent.futures
import html
import http.client
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import time

# Product-specific markers precede generic vendor names. Legacy names remain recognizable.
SIGNATURES = {
    'Fortinet': r'fortinet|fortigate|fortios|\bfgvm\w*|\bfgt[-_\d]',
    'VeloCloud': r'velocloud|vmware\s+sd.?wan|\bvelos\b',
    'Cisco': r'cisco|\bvedge\b|\bc8000v\b|\bcsr1000v\b|meraki',
    'HPE Aruba EdgeConnect': r'edgeconnect|silver\s*peak|\baruba\b',
    'Palo Alto Networks': r'palo\s*alto|pan-os|prisma\s+sd.?wan|cloudgenix',
    'Versa Networks': r'versa\s*(networks|director|flexvnf|vos|sd.?wan)|\bflexvnf\b',
    'Juniper': r'juniper|session\s+smart|128\s*technology|\b128t\b|\bvSRX\b',
    'Check Point': r'check\s*point|cloudguard|\bgaia\b',
    'Sophos': r'sophos|\bsfos\b',
    'Forcepoint': r'forcepoint|stonesoft|flexedge',
    'Barracuda': r'barracuda|cloudgen\s+firewall',
    'Peplink': r'peplink|pepwave|fusionhub',
    'Citrix / NetScaler': r'citrix\s+sd.?wan|netscaler\s+sd.?wan|\bcloudbridge\b',
    'Huawei': r'huawei|\bar1000v\b',
    'Nokia / Nuage': r'nuage|\bns[gG][-_ ]?v\b|nokia',
    'Ekinops': r'ekinops|oneaccess|oneos',
    'Cato Networks': r'cato\s*(networks|socket|vsocket)|\bvsocket\b',
    'SonicWall': r'sonicwall|sonicos|\bnsv\b',
    'WatchGuard': r'watchguard|firebox',
}
VENDORS = tuple(SIGNATURES) + ('Other',)
MODELS = r'FortiGate[- ]VM(?:64|X|[A-Z0-9-]*)?|C8000V|CSR1000V|vEdge[- ]Cloud|EdgeConnect[- ](?:Virtual|EC-V)|\bEC-V\b|VM[- ](?:50|100|300|500|700)|ION[- ]?\d+|FlexVNF|vSRX|FusionHub|AR1000V|NSG[- ]V|vSocket|NSv[- ]?\d*|Firebox[- ]V'


def classify(signals):
    matches = {}
    model = firmware = None
    for source, text in signals:
        text = html.unescape(str(text))[:65536]
        vendors = [v for v, pattern in SIGNATURES.items() if re.search('(?:'+pattern+')|'+re.escape(v), text, re.I)]
        for vendor in vendors:
            matches.setdefault(vendor, set()).add(source)
        if len(vendors) == 1:
            found = re.search(MODELS, text, re.I)
            if found and model is None:
                model = found.group(0).strip()
            version = re.search(r'(?:FortiOS|PAN-OS|ECOS|SFOS|SonicOS|OneOS|IOS\s*XE|firmware(?:\s+version)?)[\s:=v-]*(\d+\.\d+(?:\.\d+){0,2}[a-zA-Z0-9.-]*)', text, re.I)
            if not version and source == 'LLDP':
                version = re.search(r'\bv(\d+\.\d+(?:\.\d+){0,2})\b', text)
            if version and firmware is None:
                firmware = version.group(1)[:40]
    vendor = next(iter(matches)) if len(matches) == 1 else 'Other'
    sources = sorted(matches.get(vendor, []))
    return {'vendor': vendor, 'model': model if len(matches) == 1 else None,
            'firmware': firmware if len(matches) == 1 else None,
            'confidence': 'unknown' if vendor == 'Other' else ('inventory label' if sources == ['Proxmox'] else 'product hint'),
            'identity_evidence': ', '.join(sources) if sources else ('Conflicting product hints' if matches else 'No exposed product identity')}


def bound_socket(interface, source, host, port, timeout=3):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b'\0')
        sock.bind((source, 0)); sock.connect((host, port))
        return sock
    except Exception:
        sock.close(); raise


def web_signals(candidate, inventory):
    """Fixed unauthenticated GET / on the candidate's data NIC; no redirects/proxies."""
    row = next((r for r in inventory if r['interface'] == candidate['interface'] and r['up']), None)
    if not row:
        return []
    try:
        host = ipaddress.IPv4Address(candidate['gateway'])
        address = next(ipaddress.IPv4Interface(a) for a in row['addresses'] if host in ipaddress.IPv4Interface(a).network and host != ipaddress.IPv4Interface(a).ip)
    except (ValueError, StopIteration):
        return []
    signals = []
    for port in (443, 80):
        connection = None
        sock = None
        try:
            deadline = time.monotonic() + 3
            sock = bound_socket(row['interface'], str(address.ip), str(host), port)
            if port == 443:
                sock = ssl._create_unverified_context().wrap_socket(sock, server_hostname=str(host))
                cert = sock.getpeercert(binary_form=True)
                result = subprocess.run(['openssl', 'x509', '-inform', 'DER', '-noout', '-subject', '-issuer'], input=cert, capture_output=True, timeout=1)
                if result.returncode == 0:
                    signals.append(('TLS certificate', result.stdout.decode(errors='replace')))
            connection = http.client.HTTPConnection(str(host), port, timeout=3)
            connection.sock = sock
            connection.request('GET', '/', headers={'User-Agent': 'NetEm-Appliance-Discovery', 'Connection': 'close'})
            response = connection.getresponse()
            signals.append(('HTTP headers', '\n'.join(f'{k}: {v}' for k,v in response.getheaders())))
            # Header identity is sufficient on authentication/redirect responses; do not follow them.
            if response.status == 200 and time.monotonic() < deadline:
                sock.settimeout(max(.1, deadline-time.monotonic()))
                chunk = response.read1(16384)
                signals.append(('HTTP page', chunk.decode(errors='replace')))
        except (OSError, ValueError, ssl.SSLError, http.client.HTTPException, subprocess.SubprocessError):
            pass
        finally:
            if connection:
                connection.close()
            elif sock is not None:
                sock.close()
    return signals


def lldp_signals():
    """Use an existing lldpd cache when available; never install/change its daemon settings."""
    try:
        output = subprocess.run(['lldpcli', '-f', 'json', 'show', 'neighbors', 'details'], capture_output=True, text=True, timeout=2)
        if output.returncode:
            return {}
        data = json.loads(output.stdout)
        result = {}
        for block in data.get('lldp', {}).get('interface', []):
            for interface, info in block.items():
                result.setdefault(interface, []).append(('LLDP', json.dumps(info)[:16384]))
        return result
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError, TypeError):
        return {}


def enrich_candidates(candidates, inventory, vm_inventory=None, probe=False):
    metadata = {}
    for row in (vm_inventory or {}).get('interfaces', []):
        metadata.setdefault(str(row['mac']).lower(), []).append(row)
    lldp = lldp_signals() if probe else {}
    def identify(candidate):
        result = dict(candidate)
        rows = metadata.get(str(candidate.get('mac', '')).lower(), [])
        signals = []
        if len(rows) == 1:
            vm = rows[0]
            signals.append(('Proxmox', vm.get('identity_text', vm['name'])))
            result.update(vm_id=vm['vm_id'], vm_name=vm['name'], name=vm['name'])
        else:
            result['name'] = f"Appliance {candidate['gateway']}"
        # Never associate a switch advertisement with a router merely by interface.
        mac = str(candidate.get('mac', '')).lower()
        signals.extend((source, text) for source, text in lldp.get(candidate['interface'], []) if mac and mac in text.lower())
        if probe:
            signals.extend(web_signals(candidate, inventory))
        result.update(classify(signals))
        if len(rows) > 1:
            result['identity_evidence'] += '; MAC appears on multiple VMs'
        result['gateway_verified'] = False
        return result
    if probe:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(identify, candidates[:128]))
        results.extend(enrich_candidates(candidates[128:], inventory, vm_inventory, probe=False))
        return results
    return [identify(c) for c in candidates]
