import json
from pathlib import Path
import time
from unittest.mock import patch

import pytest

from trafficgen import network, maintenance, host_admin

INVENTORY = [{'interface': 'eth1', 'addresses': ['10.250.10.10/24'], 'up': True},
             {'interface': 'eth2', 'addresses': ['10.251.10.10/24'], 'up': True}]
ROUTE = {'interface': 'eth1', 'gateway': '10.250.10.1', 'target': '198.18.0.1'}


def test_route_supports_multiple_vendor_lans_without_management_changes():
    assert network.validate_route(ROUTE, inventory=INVENTORY)['source'] == '10.250.10.10'
    other = dict(ROUTE, interface='eth2', gateway='10.251.10.1')
    assert network.validate_route(other, inventory=INVENTORY)['source'] == '10.251.10.10'
    for change in ({'interface': 'eth0'}, {'interface': 'eth1;sh'}, {'gateway': '192.168.0.1'},
                   {'gateway': '10.250.10.10'}, {'gateway': '10.250.10.255'},
                   {'target': '192.168.0.191'}, {'target': '8.8.8.8'}):
        with pytest.raises(ValueError):
            network.validate_route(dict(ROUTE, **change), inventory=INVENTORY)


def test_discovery_is_candidate_evidence_not_vendor_identification():
    with patch.object(network, 'interfaces', return_value=INVENTORY), \
         patch.object(network, 'ip_json', side_effect=[
             [{'dev': 'eth1', 'gateway': '10.250.10.1'}],
             [{'dev': 'eth0', 'dst': '192.168.0.1', 'lladdr': 'aa'},
              {'dev': 'eth1', 'dst': '10.250.10.1', 'lladdr': 'bb'}]]):
        result = network.discover()
    assert len(result['candidates']) == 1
    assert result['candidates'][0]['evidence'] == 'configured gateway'
    assert 'vendor' not in result['candidates'][0]


def test_failed_path_verification_restores_previous_route(tmp_path, monkeypatch):
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', tmp_path)
    old = dict(ROUTE, source='10.250.10.10')
    (tmp_path / 'route.json').write_text(json.dumps(old))
    new = dict(ROUTE, interface='eth2', gateway='10.251.10.1')
    with patch.object(host_admin, 'validate_route', side_effect=[dict(new, source='10.251.10.10'), old]), \
         patch.object(host_admin, 'exact_routes', return_value=[]), \
         patch.object(host_admin, 'ip_json', return_value=[{'dev': 'eth2', 'gateway': '10.251.10.1'}]), \
         patch.object(host_admin, 'run', side_effect=['', ValueError('target unreachable'), '']) as command:
        with pytest.raises(ValueError, match='unreachable'):
            host_admin.apply_route(new)
    assert command.call_args_list[-1].args[0] == host_admin.route_command('replace', old)
    assert json.loads((tmp_path / 'route.json').read_text()) == old
    assert all('default' not in call.args[0] for call in command.call_args_list)


def test_unmanaged_route_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', tmp_path)
    with patch.object(host_admin, 'validate_route', return_value=dict(ROUTE, source='10.250.10.10')), \
         patch.object(host_admin, 'exact_routes', return_value=[{'protocol': 'static'}]), \
         patch.object(host_admin, 'run') as command:
        with pytest.raises(ValueError, match='unmanaged'):
            host_admin.apply_route(ROUTE)
        command.assert_not_called()


def test_queue_is_exclusive_and_host_actions_are_narrow(simulator, tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, 'ADMIN_DIR', tmp_path)
    (tmp_path / 'install-manifest.json').write_text('{}')
    settings = simulator.config['TRAFFICGEN_SETTINGS']
    result = maintenance.enqueue(settings, 'check_update')
    assert len(result['id']) == 32
    assert maintenance.status(settings)['busy']
    with pytest.raises(ValueError):
        maintenance.enqueue(settings, 'route', ROUTE)
    with pytest.raises(ValueError):
        maintenance.enqueue(settings, 'shell', {'command': 'id'})


def test_worker_rejects_symlink_requests_and_does_not_read_target(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'; runtime.mkdir()
    root = tmp_path / 'admin'; root.mkdir()
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', root)
    monkeypatch.setattr(host_admin, 'RUNTIME_DIR', runtime)
    secret = tmp_path / 'secret'; secret.write_text('must stay untouched')
    (runtime / 'admin-request.json').symlink_to(secret)
    host_admin.process_request()
    assert secret.read_text() == 'must stay untouched'
    assert json.loads((root / 'status.json').read_text())['state'] == 'failed'
    assert not (runtime / 'admin-request.json').exists()


def test_release_must_be_stable_and_newer(tmp_path, monkeypatch):
    (tmp_path / 'trafficgen').mkdir(); (tmp_path / 'trafficgen/version.txt').write_text('0.2.1')
    monkeypatch.setattr(host_admin, 'APP_DIR', tmp_path)
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', tmp_path / 'admin')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, _): return json.dumps({'tag_name':'v0.3.0', 'draft':False, 'prerelease':False}).encode()
    with patch.object(host_admin, 'urlopen', return_value=Response()):
        assert host_admin.check_release()['available']
    assert host_admin.version_tuple('netem-traffic-simulator-v0.3.0') == (0, 3, 0)
    for tag in ('main', '-evil', 'v0.3.0-rc1', 'other-v0.3.0'):
        with pytest.raises(ValueError): host_admin.version_tuple(tag)


def test_ui_and_api_require_authentication_and_idle_workloads(simulator, monkeypatch, tmp_path):
    client = simulator.test_client()
    assert client.get('/settings/system/data').status_code == 302
    assert client.get('/api/v1/network').status_code == 401
    with client.session_transaction() as session:
        session['trafficgen_admin'] = True
        session['csrf_token'] = 'token'
    assert client.post('/settings/system/action', data={'action':'check_update'}).status_code == 400
    with patch.object(maintenance, 'status', return_value={'ready':True,'busy':False,'selected':None,'release':{},'job':{}}), \
         patch.object(network, 'interfaces', return_value=INVENTORY), \
         patch.object(simulator.config['TRAFFICGEN_CONTROLLER'], 'status', return_value={'status':'running'}), \
         patch.object(maintenance, 'enqueue') as enqueue:
        response = client.post('/settings/system/action', data={'csrf_token':'token','action':'route','appliance_id':'missing'})
        assert response.status_code == 302
        enqueue.assert_not_called()


def test_update_failure_restores_application_and_venv(tmp_path, monkeypatch):
    appdir = tmp_path / 'app'; appdir.mkdir()
    (appdir / 'marker').write_text('old code')
    (appdir / '.venv').mkdir(); (appdir / '.venv/marker').write_text('old environment')
    root = tmp_path / 'admin'; root.mkdir()
    units = tmp_path / 'units'; units.mkdir()
    monkeypatch.setattr(host_admin, 'APP_DIR', appdir)
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', root)
    monkeypatch.setattr(host_admin, 'SYSTEMD_DIR', units)
    temporary_directory = host_admin.tempfile.TemporaryDirectory
    monkeypatch.setattr(host_admin.tempfile, 'TemporaryDirectory', lambda **kwargs: temporary_directory(prefix='update-test-', dir=tmp_path))
    (root / 'install-manifest.json').write_text('{"marker":"old"}')
    calls = []
    def command(args, timeout=8):
        calls.append(args)
        if args[0] == 'git':
            source = Path(args[-1]); (source / 'trafficgen').mkdir(parents=True)
            (source / 'trafficgen/version.txt').write_text('0.3.0')
        elif args[0] == 'bash':
            (appdir / 'marker').write_text('broken new code')
            (appdir / '.venv/marker').write_text('broken new environment')
            raise ValueError('installation failed')
        return ''
    with patch.object(host_admin, 'check_release', return_value={'available':True,'tag':'v0.3.0'}), \
         patch.object(host_admin, 'files_manifest', return_value={'marker':'old'}), \
         patch.object(host_admin, 'run', side_effect=command):
        with pytest.raises(ValueError, match='previous installation restored'):
            host_admin.install_release('v0.3.0')
    assert (appdir / 'marker').read_text() == 'old code'
    assert (appdir / '.venv/marker').read_text() == 'old environment'
    assert ['systemctl','restart','netem-traffic-simulator'] in calls


def test_selected_route_missing_blocks_api_workload_start(simulator):
    client = simulator.test_client()
    key = simulator.config['TRAFFICGEN_SETTINGS'].api_key_path.read_text().strip()
    with patch.object(maintenance, 'status', return_value={'busy':False,'selected':dict(ROUTE, source='10.250.10.10')}), \
         patch('trafficgen.app.ip_json', return_value=[{'dev':'eth0','gateway':'192.168.0.1'}]), \
         patch.object(simulator.config['TRAFFICGEN_CONTROLLER'], 'start') as start:
        response = client.post('/api/v1/workloads/start', json={'users':10}, headers={'Authorization':'Bearer '+key})
        assert response.status_code == 400
        assert 'route is missing or changed' in response.json['error']
        start.assert_not_called()


def test_legacy_release_tag_has_clean_display_and_keeps_real_checkout_tag(tmp_path, monkeypatch):
    (tmp_path / 'trafficgen').mkdir()
    (tmp_path / 'trafficgen/version.txt').write_text('0.2.1')
    monkeypatch.setattr(host_admin, 'APP_DIR', tmp_path)
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', tmp_path / 'admin')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, _): return json.dumps({'tag_name': 'netem-traffic-simulator-v0.3.0'}).encode()
    with patch.object(host_admin, 'urlopen', return_value=Response()):
        result = host_admin.check_release()
    assert result['available']
    assert result['display_tag'] == 'v0.3.0'
    assert result['tag'] == 'netem-traffic-simulator-v0.3.0'


def test_release_naming_matches_netem():
    config = json.loads(Path('release-please-config.json').read_text())
    assert config['include-component-in-tag'] is False
    assert config['include-v-in-tag'] is True
    assert config['include-v-in-release-name'] is True
    assert 'package-name' not in config['packages']['.']


def test_cached_release_check_is_compared_with_running_version(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, 'ADMIN_DIR', tmp_path)
    major, minor, patch_ = maintenance.version_tuple(maintenance.__version__)
    # A check cached before an install still says available; the running version decides.
    for tag, available in ((f'v{major}.{minor}.{patch_}', False), (f'v{major}.{minor}.{patch_ + 1}', True),
                           (f'v{major}.{minor - 1 if minor else 0}.0', False), ('main', False)):
        (tmp_path / 'release.json').write_text(json.dumps({'tag': tag, 'available': True}))
        assert maintenance.release_status()['available'] is available
    (tmp_path / 'release.json').write_text('[]')
    assert maintenance.release_status() == {}


def test_install_update_is_refused_without_a_newer_release(simulator, tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, 'ADMIN_DIR', tmp_path)
    client = simulator.test_client()
    with client.session_transaction() as session:
        session['trafficgen_admin'] = True
        session['csrf_token'] = 'token'
    current = 'v' + maintenance.__version__
    (tmp_path / 'release.json').write_text(json.dumps({'tag': current, 'available': True}))
    with patch.object(maintenance, 'enqueue') as enqueue:
        response = client.post('/settings/system/action', data={'csrf_token': 'token', 'action': 'install_update', 'tag': current})
        assert response.status_code == 302
        enqueue.assert_not_called()
        major, minor, patch_ = maintenance.version_tuple(current)
        newer = f'v{major}.{minor}.{patch_ + 1}'
        (tmp_path / 'release.json').write_text(json.dumps({'tag': newer, 'available': False}))
        client.post('/settings/system/action', data={'csrf_token': 'token', 'action': 'install_update', 'tag': newer})
        enqueue.assert_called_once_with(simulator.config['TRAFFICGEN_SETTINGS'], 'install_update', {'tag': newer})


def test_status_reports_update_in_progress_for_the_update_screen(simulator, tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, 'ADMIN_DIR', tmp_path)
    (tmp_path / 'install-manifest.json').write_text('{}')
    settings = simulator.config['TRAFFICGEN_SETTINGS']
    maintenance.enqueue(settings, 'check_update')
    assert maintenance.status(settings)['busy'] and not maintenance.status(settings)['updating']
    (settings.runtime_dir / 'admin-request.json').unlink()
    maintenance.enqueue(settings, 'install_update', {'tag': 'v9.9.9'})
    assert maintenance.status(settings)['updating']  # queued, before the worker picks it up
    (settings.runtime_dir / 'admin-request.json').unlink()
    (tmp_path / 'status.json').write_text(json.dumps({'action': 'install_update', 'state': 'running', 'timestamp': time.time()}))
    assert maintenance.status(settings)['updating']
    (tmp_path / 'status.json').write_text(json.dumps({'action': 'install_update', 'state': 'completed', 'timestamp': time.time()}))
    assert not maintenance.status(settings)['updating']


def test_update_worker_version_file_is_release_managed():
    # install_release compares the release tag with trafficgen/version.txt, so Release Please must rewrite that file.
    config = json.loads(Path('release-please-config.json').read_text())
    manifest = json.loads(Path('.release-please-manifest.json').read_text())
    assert config['packages']['.']['version-file'] == 'trafficgen/version.txt'
    assert 'trafficgen/version.txt' not in config['packages']['.'].get('extra-files', [])
    assert Path('trafficgen/version.txt').read_text().strip() == manifest['.']
    assert not Path('version.txt').exists()


def test_inventory_keeps_down_unaddressed_data_interfaces():
    links = [{'ifname': 'eth0', 'flags': ['UP'], 'addr_info': [{'family': 'inet', 'scope': 'global', 'local': '192.168.0.135', 'prefixlen': 24}]},
             {'ifname': 'eth1@if72', 'flags': ['BROADCAST', 'MULTICAST'],
              'addr_info': [{'family': 'inet6', 'scope': 'link', 'local': 'fe80::1', 'prefixlen': 64}]}]
    with patch.object(network, 'run', return_value=json.dumps(links)) as command:
        assert network.interfaces() == [{'interface': 'eth1', 'addresses': [], 'up': False, 'carrier': False}]
    # `ip -4` would drop eth1 entirely, so the listing must ask for every family.
    assert command.call_args.args[0] == ['ip', '-j', 'address', 'show']


def test_saved_route_reports_down_missing_address_and_wrong_kernel_path():
    selected = dict(ROUTE, source='10.250.10.10')
    for rows, expected in [([], 'missing'), ([dict(INVENTORY[0], up=False)], 'down'),
                           ([dict(INVENTORY[0], addresses=[])], 'no IPv4')]:
        with patch.object(network, 'ip_json') as kernel:
            result = network.route_status(selected, inventory=rows)
            assert result['state'] == 'error' and expected in result['message']
            kernel.assert_not_called()
    with patch.object(network, 'ip_json', return_value=[{'dev': 'eth0', 'gateway': '192.168.0.1'}]):
        assert network.route_status(selected, inventory=INVENTORY)['state'] == 'error'
    with patch.object(network, 'ip_json', return_value=[{'dev': 'eth1', 'gateway': '10.250.10.1'}]):
        assert network.route_status(selected, inventory=INVENTORY)['active']


LINKS = [{'ifname': 'eth0', 'link_type': 'ether', 'flags': ['UP'],
          'addr_info': [{'family': 'inet', 'local': '192.168.0.135', 'prefixlen': 24}]},
         {'ifname': 'eth1', 'link_type': 'ether', 'flags': [], 'addr_info': []}]


def test_interface_recovery_protects_management_and_existing_addresses():
    payload = {'interface': 'eth1', 'address': '10.250.10.10/24'}
    assert network.validate_interface(payload, links=LINKS) == payload
    for change in ({'interface': 'eth0'}, {'interface': 'lo'}, {'interface': 'missing'},
                   {'address': '192.168.0.10/24'}, {'address': '10.250.10.10'},
                   {'address': '10.250.10.0/24'}, {'address': '198.18.0.1/24'}, {'interface': 'eth1;sh'}):
        with pytest.raises(ValueError):
            network.validate_interface(dict(payload, **change), links=LINKS)
    addressed = [LINKS[0], dict(LINKS[1], addr_info=[{'family': 'inet', 'local': '10.250.20.10', 'prefixlen': 24}])]
    with pytest.raises(ValueError, match='different'):
        network.validate_interface(payload, links=addressed)


def test_recovery_enables_addresses_and_persists_without_default_route_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', tmp_path)
    payload = {'interface': 'eth1', 'address': '10.250.10.10/24'}
    with patch.object(host_admin, 'validate_interface', return_value=payload), \
         patch.object(host_admin, 'address_table', return_value=LINKS), \
         patch.object(host_admin, 'interfaces', return_value=INVENTORY), \
         patch.object(host_admin, 'run') as command:
        host_admin.configure_interface(payload)
    assert json.loads((tmp_path / 'interfaces.json').read_text()) == {'eth1': payload}
    assert [call.args[0] for call in command.call_args_list] == [
        ['ip', 'link', 'set', 'dev', 'eth1', 'up'],
        ['ip', '-4', 'address', 'add', '10.250.10.10/24', 'dev', 'eth1']]


def test_failed_recovery_restores_previous_link_and_removes_added_address(tmp_path, monkeypatch):
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', tmp_path)
    payload = {'interface': 'eth1', 'address': '10.250.10.10/24'}
    with patch.object(host_admin, 'validate_interface', return_value=payload), \
         patch.object(host_admin, 'address_table', return_value=LINKS), \
         patch.object(host_admin, 'interfaces', return_value=[]), \
         patch.object(host_admin, 'run') as command:
        with pytest.raises(ValueError, match='did not become configured'):
            host_admin.configure_interface(payload)
    assert command.call_args_list[-2].args[0] == ['ip', '-4', 'address', 'del', '10.250.10.10/24', 'dev', 'eth1']
    assert command.call_args_list[-1].args[0] == ['ip', 'link', 'set', 'dev', 'eth1', 'down']
    assert not (tmp_path / 'interfaces.json').exists()


def test_boot_restores_interface_before_target_route(tmp_path, monkeypatch):
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', tmp_path)
    config = {'interface': 'eth1', 'address': '10.250.10.10/24'}
    (tmp_path / 'interfaces.json').write_text(json.dumps({'eth1': config}))
    (tmp_path / 'route.json').write_text(json.dumps(ROUTE))
    calls = []
    with patch.object(host_admin, 'configure_interface', side_effect=lambda *_args, **_kwargs: calls.append('interface')), \
         patch.object(host_admin, 'apply_route', side_effect=lambda *_args, **_kwargs: calls.append('route')):
        host_admin.restore_network()
    assert calls == ['interface', 'route']


def test_gui_reports_stale_selection_and_queues_idle_interface_recovery(simulator, tmp_path, monkeypatch):
    from trafficgen import app as app_module
    monkeypatch.setattr(maintenance, 'ADMIN_DIR', tmp_path)
    (tmp_path / 'install-manifest.json').write_text('{}')
    (tmp_path / 'route.json').write_text(json.dumps(dict(ROUTE, source='10.250.10.10')))
    client = simulator.test_client()
    with client.session_transaction() as session:
        session['trafficgen_admin'] = True
        session['csrf_token'] = 'token'
    with patch.object(app_module, 'discover', return_value={'interfaces': [{'interface': 'eth1', 'up': False, 'carrier': False, 'addresses': []}], 'candidates': []}):
        state = client.get('/settings/system/data').json
        assert state['route_health']['state'] == 'error'
        page = client.get('/settings/system').text
        assert 'eth1 is down' in page and 'Enable &amp; save interface' in page
        assert 'value="eth1"' in page
    data = {'csrf_token': 'token', 'action': 'configure_interface', 'interface': 'eth1', 'address': '10.250.10.10/24'}
    with patch.object(app_module, 'validate_interface', return_value={'interface': 'eth1', 'address': '10.250.10.10/24'}):
        response = client.post('/settings/system/action', data=data)
    assert response.status_code == 302
    request = json.loads((simulator.config['TRAFFICGEN_SETTINGS'].runtime_dir / 'admin-request.json').read_text())
    assert request['action'] == 'configure_interface'
    assert request['payload'] == {'interface': 'eth1', 'address': '10.250.10.10/24'}


def test_selected_route_remembers_the_interface_address():
    assert network.validate_route(ROUTE, inventory=INVENTORY)['address'] == '10.250.10.10/24'


def test_readiness_with_a_saved_route():
    selected = dict(ROUTE, source='10.250.10.10', address='10.250.10.10/24')
    with patch.object(network, 'ip_json', return_value=[{'dev': 'eth1', 'gateway': '10.250.10.1'}]):
        ready = network.path_readiness(selected, inventory=INVENTORY)
    assert ready['ready'] and not ready['repairable'] and ready['saved_route']
    assert ready['path'] == {'interface': 'eth1', 'gateway': '10.250.10.1', 'target': '198.18.0.1', 'source': '10.250.10.10'}
    # Proxmox brought eth1 up without its address: the saved address makes it repairable.
    down = [dict(INVENTORY[0], up=False, addresses=[])]
    broken = network.path_readiness(selected, inventory=down)
    assert not broken['ready'] and broken['repairable'] and 'can restore' in broken['message']
    # An old saved route has no address and no saved interface configuration: nothing to restore it from.
    old = dict(selected)
    old.pop('address')
    assert not network.path_readiness(old, inventory=down)['repairable']
    assert network.path_readiness(old, inventory=down, saved_interfaces={'eth1': {'address': '10.250.10.10/24'}})['repairable']
    # A NIC that is gone needs Proxmox, not the simulator.
    assert not network.path_readiness(selected, inventory=[])['repairable']


def test_readiness_without_a_saved_route_catches_the_management_bypass():
    appliance = dict(ROUTE, id='a1', name='FortiGate')
    via_management = [{'dev': 'eth0', 'gateway': '192.168.0.1', 'prefsrc': '192.168.0.135'}]
    with patch.object(network, 'ip_json', return_value=via_management):
        one = network.path_readiness(None, inventory=INVENTORY, appliances=[appliance])
        two = network.path_readiness(None, inventory=INVENTORY, appliances=[appliance, dict(appliance, id='a2')])
        single_nic = network.path_readiness(None, inventory=[])
    assert not one['ready'] and one['repairable'] and 'bypass the appliance' in one['message'] and 'FortiGate' in one['message']
    assert not two['ready'] and not two['repairable'] and 'Select an appliance' in two['message']
    assert single_nic['ready']
    with patch.object(network, 'ip_json', return_value=[{'dev': 'eth1', 'gateway': '10.250.10.1'}]):
        assert network.path_readiness(None, inventory=INVENTORY)['ready']


def test_repair_restores_the_address_before_the_route_and_verifies_the_target(tmp_path, monkeypatch):
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', tmp_path)
    saved = dict(ROUTE, source='10.250.10.10', address='10.250.10.10/24')
    (tmp_path / 'route.json').write_text(json.dumps(saved))
    calls = []
    with patch.object(host_admin, 'configure_interface', side_effect=lambda config, persist: calls.append(('interface', config, persist))), \
         patch.object(host_admin, 'apply_route', side_effect=lambda route, verify: calls.append(('route', route['gateway'], verify))):
        result = host_admin.repair_network({})
    assert calls == [('interface', {'interface': 'eth1', 'address': '10.250.10.10/24'}, False), ('route', '10.250.10.1', True)]
    assert 'restored' in result['message']


def test_repair_selects_the_one_saved_appliance_or_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', tmp_path)
    with patch.object(host_admin, 'configure_interface'), patch.object(host_admin, 'apply_route') as route:
        host_admin.repair_network({'appliance': dict(ROUTE, id='a1')})
        assert route.call_args.kwargs == {'verify': True}
        with pytest.raises(ValueError, match='No appliance route'):
            host_admin.repair_network({})
    with patch.object(host_admin, 'configure_interface'), \
         patch.object(host_admin, 'apply_route', side_effect=ValueError('target unreachable')):
        with pytest.raises(ValueError, match='unreachable'):
            host_admin.repair_network({'appliance': ROUTE})


def test_boot_restore_uses_the_saved_route_address(tmp_path, monkeypatch):
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', tmp_path)
    (tmp_path / 'route.json').write_text(json.dumps(dict(ROUTE, address='10.250.10.10/24')))
    with patch.object(host_admin, 'configure_interface') as interface, patch.object(host_admin, 'apply_route') as route:
        host_admin.restore_network()
    assert interface.call_args.args[0] == {'interface': 'eth1', 'address': '10.250.10.10/24'}
    assert route.call_args.kwargs == {'verify': False}


def test_workload_refuses_to_bypass_the_appliance_over_management(simulator, monkeypatch):
    from trafficgen import app as app_module
    client = simulator.test_client()
    key = simulator.config['TRAFFICGEN_SETTINGS'].api_key_path.read_text().strip()
    monkeypatch.setattr(app_module, 'target_egress', lambda host: {'dev': 'eth0', 'gateway': '192.168.0.1'})
    with patch.object(maintenance, 'status', return_value={'busy': False, 'selected': None}), \
         patch.object(app_module, 'interfaces', return_value=INVENTORY), \
         patch.object(simulator.config['TRAFFICGEN_CONTROLLER'], 'start') as start:
        response = client.post('/api/v1/workloads/start', json={'users': 10}, headers={'Authorization': 'Bearer ' + key})
        assert response.status_code == 400 and 'bypass the appliance' in response.json['error']
        start.assert_not_called()
        # A single-NIC simulator reaches the appliance over eth0 and may start.
        with patch.object(app_module, 'interfaces', return_value=[]):
            start.return_value = {'status': 'starting'}
            assert client.post('/api/v1/workloads/start', json={'users': 10}, headers={'Authorization': 'Bearer ' + key}).status_code == 201


def test_readiness_and_repair_api(simulator, tmp_path, monkeypatch):
    from trafficgen import app as app_module
    monkeypatch.setattr(maintenance, 'ADMIN_DIR', tmp_path)
    (tmp_path / 'install-manifest.json').write_text('{}')
    (tmp_path / 'route.json').write_text(json.dumps(dict(ROUTE, source='10.250.10.10', address='10.250.10.10/24')))
    client = simulator.test_client()
    headers = {'Authorization': 'Bearer ' + simulator.config['TRAFFICGEN_SETTINGS'].api_key_path.read_text().strip()}
    assert client.get('/api/v1/network/readiness').status_code == 401
    down = [dict(INVENTORY[0], up=False, addresses=[])]
    with patch.object(app_module, 'interfaces', return_value=down):
        state = client.get('/api/v1/network/readiness', headers=headers).json
        assert not state['ready'] and state['repairable'] and 'target' not in state
        response = client.post('/api/v1/network/repair', headers=headers)
        assert response.status_code == 202 and response.json['action'] == 'repair'
        assert json.loads((simulator.config['TRAFFICGEN_SETTINGS'].runtime_dir / 'admin-request.json').read_text())['payload'] == {}
        assert client.post('/api/v1/network/repair', headers=headers).status_code == 409
    (simulator.config['TRAFFICGEN_SETTINGS'].runtime_dir / 'admin-request.json').unlink()
    with patch.object(app_module, 'interfaces', return_value=INVENTORY), \
         patch('trafficgen.network.ip_json', return_value=[{'dev': 'eth1', 'gateway': '10.250.10.1'}]), \
         patch.object(app_module, 'target_health', return_value=(False, 'The controlled target at 198.18.0.1 did not answer (timed out).')):
        state = client.get('/api/v1/network/readiness', headers=headers).json
        assert not state['ready'] and not state['repairable'] and 'appliance policy' in state['message']
        assert state['target']['ok'] is False
        with patch.object(app_module, 'target_health', return_value=(True, 'answers')):
            assert client.get('/api/v1/network/readiness', headers=headers).json['ready']
            assert client.post('/api/v1/network/repair', headers=headers).json['repair'] == 'not_needed'


def test_watchdog_repairs_only_saved_routes_and_backs_off(simulator):
    from trafficgen import app as app_module
    events = []

    class Stop:
        def __init__(self, rounds):
            self.rounds = rounds

        def wait(self, _delay):
            self.rounds -= 1
            return self.rounds < 0

    states = {'saved': {'ready': False, 'repairable': True, 'saved_route': True, 'busy': False, 'message': 'eth1 is down.'},
              'unsaved': {'ready': False, 'repairable': True, 'saved_route': False, 'busy': False, 'message': 'bypass'}}
    for name, expected in (('saved', 1), ('unsaved', 0)):
        events.clear()
        with patch.dict(simulator.config, {'TRAFFICGEN_NETWORK_READINESS': lambda check_target: states[name],
                                           'TRAFFICGEN_REPAIR': lambda: events.append('repair')}):
            app_module.path_watchdog(simulator, Stop(3), first=0, interval=0, backoff=3600, log=lambda message: None)
        # Three rounds with a broken saved route queue one repair, then back off.
        assert events.count('repair') == expected
