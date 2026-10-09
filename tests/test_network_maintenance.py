import json
from pathlib import Path
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
    for tag in ('main', '-evil', 'v0.3.0-rc1'):
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
