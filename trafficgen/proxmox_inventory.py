"""Read-only Proxmox inventory on the configured management LAN."""
from __future__ import annotations
import concurrent.futures
import hashlib
import http.client
import ipaddress
import json
import re
import ssl
import uuid
from .network import ip_json
from .appliance_identity import bound_socket, classify


def validate_config(payload, management='eth0', links=None):
    try:
        host = ipaddress.IPv4Address(payload.get('host', ''))
        token_id = str(payload.get('token_id', ''))
        secret = str(uuid.UUID(str(payload.get('token_secret', ''))))
    except (ValueError, AttributeError) as exc:
        raise ValueError('Provide a management IPv4 address, token ID and Proxmox token secret.') from exc
    if not re.fullmatch(r'[\w.-]+@[\w.-]+![\w.-]+', token_id, re.ASCII):
        raise ValueError('Token ID must use user@realm!token format.')
    links = links if links is not None else ip_json('address', 'show')
    addresses = [ipaddress.IPv4Interface(f"{a['local']}/{a['prefixlen']}")
                 for link in links if link.get('ifname', '').split('@')[0] == management and 'UP' in link.get('flags', [])
                 for a in link.get('addr_info', []) if a.get('family') == 'inet' and a.get('scope', 'global') == 'global']
    address = next((a for a in addresses if host in a.network and host != a.ip and host not in (a.network.network_address, a.network.broadcast_address)), None)
    if not address:
        raise ValueError('Proxmox must be reachable on the directly connected management IPv4 LAN.')
    pin = str(payload.get('fingerprint', '')).lower()
    if pin and not re.fullmatch('[a-f0-9]{64}', pin):
        raise ValueError('Invalid Proxmox certificate fingerprint.')
    return {'host': str(host), 'token_id': token_id, 'token_secret': secret, 'source': str(address.ip),
            'interface': management, 'allow_self_signed': payload.get('allow_self_signed') in (True, 'on'), 'fingerprint': pin}


class Client:
    def __init__(self, config):
        self.config = config
        self.fingerprint = config.get('fingerprint', '')

    def get(self, path):
        if path != '/cluster/resources?type=vm' and not re.fullmatch(r'/nodes/[A-Za-z0-9_.-]+/qemu/\d+/config', path):
            raise ValueError('Unsupported inventory endpoint.')
        c = self.config
        connection = None
        sock = None
        try:
            sock = bound_socket(c['interface'], c['source'], c['host'], 8006, timeout=5)
            context = ssl._create_unverified_context() if c['allow_self_signed'] else ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=c['host'])
            fingerprint = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
            if self.fingerprint and self.fingerprint != fingerprint:
                raise ValueError('Proxmox certificate changed. Remove and reconnect inventory after verifying the certificate.')
            self.fingerprint = fingerprint
            connection = http.client.HTTPConnection(c['host'], 8006, timeout=5)
            connection.sock = sock
            connection.request('GET', '/api2/json'+path, headers={
                'Authorization': f"PVEAPIToken={c['token_id']}={c['token_secret']}", 'Connection': 'close'})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f'Proxmox inventory returned HTTP {response.status}. Check PVEAuditor permissions for both user and token.')
            raw = response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024:
                raise ValueError('Inventory response is too large.')
            return json.loads(raw)['data']
        except (OSError, http.client.HTTPException, KeyError, json.JSONDecodeError) as exc:
            raise ValueError('Cannot read Proxmox inventory. Check management connectivity, certificate and API token.') from exc
        finally:
            if connection: connection.close()
            elif sock: sock.close()


def collect(config):
    client = Client(config)
    resources = client.get('/cluster/resources?type=vm')
    vms = [r for r in resources if r.get('type') == 'qemu' and not r.get('template')]
    if len(vms) > 128:
        raise ValueError('Inventory is limited to 128 visible VMs; narrow the read-only token scope.')
    errors = []
    def read_vm(vm):
        node, vm_id = str(vm.get('node', '')), str(vm.get('vmid', ''))
        if not re.fullmatch('[A-Za-z0-9_.-]+', node) or not vm_id.isdigit():
            return []
        try:
            data = client.get(f'/nodes/{node}/qemu/{vm_id}/config')
        except ValueError:
            errors.append(f'VM {vm_id} configuration is unavailable.');return []
        name = str(data.get('name') or vm.get('name') or f'VM {vm_id}')[:80]
        identity = classify([('Proxmox', name+' '+str(data.get('description', ''))[:8192])])
        # Cache product fields, never full VM descriptions (which may contain credentials).
        text = name+' '+(identity['vendor'] if identity['vendor'] != 'Other' else '')
        if identity['model']: text += ' '+identity['model']
        if identity['firmware']: text += ' firmware '+identity['firmware']
        rows = []
        for key,value in data.items():
            if not re.fullmatch('net\\d+', key): continue
            match = re.search(r'(?:^|,)(?:virtio|e1000|e1000e|rtl8139|vmxnet3|vmxnet2|ne2k_pci|i82551|i82557b|i82559er)=([a-fA-F0-9:]{17})(?:,|$)', str(value))
            if match:
                rows.append({'mac': match.group(1).lower(), 'vm_id': int(vm_id), 'name': name, 'node': node, 'identity_text': text})
        return rows
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        interfaces = [item for rows in pool.map(read_vm, vms) for item in rows]
    return {'interfaces': interfaces, 'vm_count': len(vms), 'warnings': errors[:20], 'fingerprint': client.fingerprint}
