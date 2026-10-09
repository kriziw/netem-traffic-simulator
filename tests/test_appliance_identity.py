import json
from unittest.mock import patch
import pytest
from trafficgen import appliance_identity as identity, proxmox_inventory as proxmox, host_admin, maintenance

LINKS = [{'ifname': 'eth0', 'flags': ['UP'], 'addr_info': [{'family': 'inet', 'local': '192.168.0.135', 'prefixlen': 24}]}]
CONFIG = {'host': '192.168.0.151', 'token_id': 'inventory@pve!discovery', 'token_secret': '12345678-1234-1234-1234-123456789abc', 'allow_self_signed': True}
CANDIDATE = {'interface': 'eth1', 'gateway': '10.250.10.1', 'mac': 'bc:24:11:cc:51:82', 'evidence': 'LAN neighbor'}
INVENTORY = [{'interface': 'eth1', 'up': True, 'addresses': ['10.250.10.10/24']}]


@pytest.mark.parametrize('vendor,marker', [
    ('Fortinet','FortiGate-VM64 FortiOS 7.6.7'), ('VeloCloud','VMware SD-WAN by VeloCloud'),
    ('Cisco','Cisco C8000V IOS XE 17.15.1'), ('HPE Aruba EdgeConnect','Silver Peak EdgeConnect EC-V ECOS 9.3.1'),
    ('Palo Alto Networks','Palo Alto Networks VM-300 PAN-OS 11.1.2'), ('Versa Networks','Versa FlexVNF'),
    ('Juniper','Juniper Session Smart Router'), ('Check Point','Check Point CloudGuard'), ('Sophos','Sophos SFOS'),
    ('Forcepoint','Forcepoint FlexEdge'), ('Barracuda','Barracuda CloudGen Firewall'), ('Peplink','Peplink FusionHub'),
    ('Citrix / NetScaler','Citrix SD-WAN'), ('Huawei','Huawei AR1000V'), ('Nokia / Nuage','Nuage NSG-V'),
    ('Ekinops','Ekinops OneOS'), ('Cato Networks','Cato Networks vSocket'), ('SonicWall','SonicWall NSv'), ('WatchGuard','WatchGuard FireboxV')])
def test_major_virtual_appliance_brand_hints(vendor, marker):
    assert identity.classify([('HTTP page', marker)])['vendor'] == vendor


def test_model_and_firmware_are_optional_observed_hints():
    result = identity.classify([('LLDP', 'FortiGate-VM64 v7.6.7,build1234')])
    assert result['model'] == 'FortiGate-VM64'
    assert result['firmware'] == '7.6.7'
    unknown = identity.classify([('HTTP headers', 'Server: nginx; VMware virtual ethernet')])
    assert unknown['vendor'] == 'Other' and unknown['firmware'] is None
    assert identity.classify([('HTTP page','Fortinet Cisco')])['confidence'] == 'unknown'


def test_mac_match_prefills_vm_name_without_guessing_gateway_role():
    vm = {'interfaces': [{'mac': CANDIDATE['mac'], 'vm_id': 1201, 'name': 'FortiGate lab', 'identity_text': 'FortiGate-VM64 FortiOS 7.6.7'}]}
    result = identity.enrich_candidates([CANDIDATE], INVENTORY, vm)[0]
    assert result['name'] == 'FortiGate lab' and result['vm_id'] == 1201
    assert result['vendor'] == 'Fortinet' and result['confidence'] == 'inventory label'
    assert result['gateway'] == '10.250.10.1' and not result['gateway_verified']
    vm['interfaces'].append(dict(vm['interfaces'][0], vm_id=1202))
    result = identity.enrich_candidates([CANDIDATE], INVENTORY, vm)[0]
    assert result['vendor'] == 'Other' and 'multiple VMs' in result['identity_evidence']


def test_web_probes_do_not_contact_off_lan_hosts_or_down_interfaces():
    with patch.object(identity, 'bound_socket') as connect:
        assert identity.web_signals(dict(CANDIDATE, gateway='192.168.0.151'), INVENTORY) == []
        assert identity.web_signals(CANDIDATE, [dict(INVENTORY[0], up=False)]) == []
        connect.assert_not_called()


def test_proxmox_configuration_is_management_only_and_rejects_header_injection():
    result = proxmox.validate_config(CONFIG, links=LINKS)
    assert result['source'] == '192.168.0.135' and result['interface'] == 'eth0'
    for change in ({'host':'8.8.8.8'}, {'host':'10.250.10.1'}, {'host':'192.168.0.135'}, {'token_id':'user\nHeader: bad'}, {'token_secret':'bad'}, {'fingerprint':'bad'}):
        with pytest.raises(ValueError): proxmox.validate_config(dict(CONFIG, **change), links=LINKS)


def test_inventory_reads_only_vm_config_and_matches_virtio_mac():
    calls = []
    def get(client, path):
        calls.append(path);client.fingerprint = 'a'*64
        if path.startswith('/cluster/'):
            return [{'type':'qemu','vmid':1201,'node':'pmox03','name':'Branch'}, {'type':'lxc','vmid':100,'node':'pmox03'}]
        return {'name':'FortiGate lab','description':'FortiGate-VM64 FortiOS 7.6.7 password=DO_NOT_CACHE', 'net2':'virtio=BC:24:11:CC:51:82,bridge=vmbr3'}
    with patch.object(proxmox.Client, 'get', get):
        result = proxmox.collect(CONFIG)
    assert calls == ['/cluster/resources?type=vm', '/nodes/pmox03/qemu/1201/config']
    assert result['interfaces'][0]['mac'] == CANDIDATE['mac']
    assert 'DO_NOT_CACHE' not in json.dumps(result)
    assert identity.classify([('Proxmox', result['interfaces'][0]['identity_text'])])['firmware'] == '7.6.7'


def test_inventory_secret_is_root_only_and_never_in_status(tmp_path, monkeypatch):
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', tmp_path)
    result = {'interfaces': [], 'vm_count': 0, 'warnings': [], 'fingerprint':'a'*64}
    with patch.object(host_admin, 'validate_config', return_value=CONFIG.copy()), patch.object(host_admin, 'collect', return_value=result):
        host_admin.inventory_configure(CONFIG)
    assert (tmp_path/'proxmox-secret.json').stat().st_mode & 0o777 == 0o600
    assert CONFIG['token_secret'] not in (tmp_path/'proxmox-status.json').read_text()
    host_admin.inventory_disconnect()
    assert not (tmp_path/'proxmox-secret.json').exists()


def test_failed_inventory_refresh_does_not_reuse_stale_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', tmp_path)
    (tmp_path/'proxmox-secret.json').write_text(json.dumps(CONFIG))
    (tmp_path/'proxmox-inventory.json').write_text(json.dumps({'interfaces':[{'mac':CANDIDATE['mac'],'vm_id':1201,'name':'FortiGate'}]}))
    with patch.object(host_admin, 'validate_config', return_value=CONFIG), patch.object(host_admin, 'collect', side_effect=ValueError('Unavailable')), \
         patch.object(host_admin, 'discover', return_value={'candidates':[CANDIDATE],'interfaces':INVENTORY}), \
         patch.object(identity, 'web_signals', return_value=[]), patch.object(identity, 'lldp_signals', return_value={}):
        result = host_admin.appliance_scan()
    assert result['candidates'][0]['vendor'] == 'Other'
    assert result['warning'] == 'Unavailable'
    assert not json.loads((tmp_path/'proxmox-status.json').read_text())['connected']
