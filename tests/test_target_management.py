import hashlib
import json
from pathlib import Path
import socket
import ssl
import subprocess
import threading
from unittest.mock import patch

import pytest

from trafficgen import __version__, host_admin, maintenance, remote_target, target_manager

KEY = 'ntt_' + 'a' * 64
PIN = 'a' * 64
LINKS = [{'ifname': 'eth0', 'flags': ['UP'], 'addr_info': [
    {'family': 'inet', 'scope': 'global', 'local': '192.168.10.10', 'prefixlen': 24}]}]
CONFIG = {'host': '192.168.10.20', 'api_key': KEY, 'fingerprint': PIN}


@pytest.fixture
def manager(tmp_path):
    config = tmp_path / 'config'; config.mkdir()
    runtime = tmp_path / 'runtime'; runtime.mkdir()
    admin = tmp_path / 'admin'; admin.mkdir()
    (config / 'api.key').write_text(KEY)
    (admin / 'install-manifest.json').write_text('{}')
    app = target_manager.create_app(config, runtime, admin)
    app.config['TESTING'] = True
    return app, config, runtime, admin


def auth(key=KEY):
    return {'Authorization': 'Bearer ' + key}


def test_manager_authentication_rotation_and_narrow_api(manager):
    app, config, runtime, admin = manager
    client = app.test_client()
    assert client.get('/api/v1/status').status_code == 401
    assert client.get('/api/v1/status', headers=auth('bad')).status_code == 401
    assert client.get('/api/v1/status', headers={'Authorization': KEY}).status_code == 401
    response = client.get('/api/v1/status', headers=auth())
    assert response.status_code == 200
    assert response.json['service'] == remote_target.SERVICE
    assert response.json['version'] == __version__
    assert response.headers['Cache-Control'] == 'no-store'
    assert KEY not in response.text
    assert client.post('/api/v1/check', json={}, headers=auth()).status_code == 202
    assert json.loads((runtime / 'admin-request.json').read_text())['action'] == 'check_update'
    assert client.post('/api/v1/check', json={}, headers=auth()).status_code == 409
    assert client.post('/api/v1/check', json={'repository': 'evil'}, headers=auth()).status_code == 400
    assert client.post('/api/v1/check', json=[], headers=auth()).status_code == 400
    assert client.post('/api/v1/update', json={'tag': 'main'}, headers=auth()).status_code == 409
    assert client.post('/api/v1/update', json={'tag': 'v9.0.0', 'command': 'id'}, headers=auth()).status_code == 400
    assert client.get('/static/app.js', headers=auth()).status_code == 404
    assert client.post('/api/v1/route', json={}, headers=auth()).status_code == 404
    (config / 'api.key').write_text('ntt_' + 'b' * 64)
    assert client.get('/api/v1/status', headers=auth()).status_code == 401
    assert client.get('/api/v1/status', headers=auth('ntt_' + 'b' * 64)).status_code == 200
    (config / 'api.key').unlink()
    assert client.get('/api/v1/status', headers=auth()).status_code == 401


def test_manager_install_requires_checked_newer_matching_release(manager):
    app, config, runtime, admin = manager
    client = app.test_client()
    assert client.post('/api/v1/update', json={'tag': 'v99.0.0'}, headers=auth()).status_code == 409
    (admin / 'release.json').write_text(json.dumps({'tag': 'v99.0.0', 'available': True}))
    assert client.post('/api/v1/update', json={'tag': 'v98.0.0'}, headers=auth()).status_code == 409
    assert client.post('/api/v1/update', json={'tag': 'v99.0.0'}, headers=auth()).status_code == 202
    queued = json.loads((runtime / 'admin-request.json').read_text())
    assert queued['payload'] == {'tag': 'v99.0.0'}
    assert queued['action'] == 'install_update'


def test_target_connection_restricted_to_management_lan_and_explicit_certificate():
    config = remote_target.validate_config(CONFIG, links=LINKS)
    assert config['source'] == '192.168.10.10'
    assert config['interface'] == 'eth0'
    for change in ({'host': '8.8.8.8'}, {'host': '192.168.10.10'}, {'host': '192.168.10.255'},
                   {'host': 'https://192.168.10.20:8091'}, {'api_key': 'short'},
                   {'fingerprint': ''}, {'fingerprint': 'invalid'}):
        with pytest.raises(ValueError):
            remote_target.validate_config(dict(CONFIG, **change), links=LINKS)
    with pytest.raises(ValueError):
        remote_target.validate_config(CONFIG, management='eth1', links=LINKS)


def test_certificate_mismatch_sends_no_api_key():
    sock = type('Socket', (), {'getpeercert': lambda self, binary_form: b'wrong cert', 'close': lambda self: None})()
    context = type('Context', (), {'wrap_socket': lambda self, sock, server_hostname: sock})()
    with patch.object(remote_target, 'bound_socket', return_value=sock), \
         patch.object(ssl, '_create_unverified_context', return_value=context), \
         patch.object(remote_target.http.client, 'HTTPConnection') as connection:
        with pytest.raises(ValueError, match='certificate mismatch'):
            remote_target.Client({**CONFIG, 'interface': 'eth0', 'source': '192.168.10.10'}).request('GET', '/api/v1/status')
        connection.assert_not_called()


def test_real_pinned_https_manager_client_and_queue(manager, monkeypatch):
    from werkzeug.serving import make_server

    app, config, runtime, admin = manager
    cert, key = config / 'tls.crt', config / 'tls.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
                   check=True, capture_output=True)
    pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
    server = make_server('127.0.0.1', 0, app, threaded=True, ssl_context=(str(cert), str(key)))
    monkeypatch.setattr(remote_target, 'PORT', server.server_port)
    monkeypatch.setattr(remote_target, 'bound_socket',
                        lambda interface, source, host, port, timeout: socket.create_connection((host, port), timeout))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    client = remote_target.Client({**CONFIG, 'host': '127.0.0.1', 'source': '127.0.0.1', 'interface': 'lo', 'fingerprint': pin})
    try:
        assert client.request('GET', '/api/v1/status')['ready']
        job = client.request('POST', '/api/v1/check', {})
        assert json.loads((runtime / 'admin-request.json').read_text())['id'] == job['job_id']
        with pytest.raises(ValueError, match='HTTP 409'):
            client.request('POST', '/api/v1/check', {})
        with pytest.raises(ValueError, match='Unsupported'):
            client.request('GET', '/arbitrary')
    finally:
        server.shutdown(); thread.join(timeout=5); server.server_close()


def remote_state(version=__version__, state='completed', job_id='b' * 32):
    return {'service': remote_target.SERVICE, 'version': version, 'ready': True, 'busy': False,
            'release': {'tag': 'v99.0.0', 'available': True}, 'job': {'id': job_id, 'state': state}}


def test_proxy_preserves_credentials_privately_and_handles_target_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_target, 'validate_config', lambda payload, management: CONFIG)
    client = type('Client', (), {})()
    with patch.object(remote_target, 'Client', return_value=client), \
         patch.object(client, 'request', create=True, return_value=remote_state()):
        remote_target.perform('configure_target', CONFIG, tmp_path)
    assert json.loads((tmp_path / remote_target.SECRET_FILE).read_text())['api_key'] == KEY
    assert (tmp_path / remote_target.SECRET_FILE).stat().st_mode & 0o777 == 0o600
    assert KEY not in (tmp_path / remote_target.STATUS_FILE).read_text()
    replies = [remote_state(), {'job_id': 'b' * 32}, ValueError('restarting'),
               remote_state(version='99.0.0')]
    with patch.object(remote_target, 'Client', return_value=client), \
         patch.object(client, 'request', create=True, side_effect=replies) as request, \
         patch.object(remote_target.time, 'sleep'):
        assert 'verified' in remote_target.perform('target_install', {'tag': 'v99.0.0'}, tmp_path)['message']
        assert request.call_args_list[1].args == ('POST', '/api/v1/update', {'tag': 'v99.0.0'})
    remote_target.perform('disconnect_target', {}, tmp_path)
    assert not (tmp_path / remote_target.SECRET_FILE).exists()


def test_proxy_does_not_claim_update_success_for_wrong_running_version(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_target, 'validate_config', lambda payload, management: CONFIG)
    with patch.object(remote_target.Client, 'request', side_effect=[
        remote_state(), {'job_id': 'b' * 32}, remote_state(version=__version__)]), \
         patch.object(remote_target.time, 'sleep'):
        with pytest.raises(ValueError, match='does not match'):
            remote_target.perform('target_install', {'tag': 'v99.0.0'}, tmp_path)


def test_target_worker_rejects_route_actions_even_with_a_forged_queue_file(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'; runtime.mkdir()
    admin = tmp_path / 'admin'; admin.mkdir()
    monkeypatch.setattr(host_admin, 'TARGET_ROLE', True)
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', admin)
    monkeypatch.setattr(host_admin, 'RUNTIME_DIR', runtime)
    (runtime / 'admin-request.json').write_text(json.dumps({'id': 'a' * 32, 'action': 'route', 'payload': {}}))
    with patch.object(host_admin, 'apply_route') as route:
        host_admin.process_request()
        route.assert_not_called()
    assert json.loads((admin / 'status.json').read_text())['state'] == 'failed'


def test_target_update_health_failure_restores_code_venv_and_services(tmp_path, monkeypatch):
    appdir = tmp_path / 'app'; appdir.mkdir()
    (appdir / 'marker').write_text('old')
    (appdir / '.venv').mkdir(); (appdir / '.venv/marker').write_text('old env')
    admin = tmp_path / 'admin'; admin.mkdir()
    (admin / 'install-manifest.json').write_text('{"marker":"old"}')
    units = tmp_path / 'units'; units.mkdir()
    monkeypatch.setattr(host_admin, 'TARGET_ROLE', True)
    monkeypatch.setattr(host_admin, 'SERVICE_NAME', 'netem-traffic-target')
    monkeypatch.setattr(host_admin, 'APP_DIR', appdir)
    monkeypatch.setattr(host_admin, 'ADMIN_DIR', admin)
    monkeypatch.setattr(host_admin, 'SYSTEMD_DIR', units)
    original = host_admin.tempfile.TemporaryDirectory
    monkeypatch.setattr(host_admin.tempfile, 'TemporaryDirectory',
                        lambda **kwargs: original(prefix='target-test-', dir=tmp_path))
    calls = []
    def run(args, timeout=8):
        calls.append(args)
        if args[0] == 'git':
            source = Path(args[-1]); (source / 'trafficgen').mkdir(parents=True)
            (source / 'trafficgen/version.txt').write_text('99.0.0')
        if args[0] == 'bash':
            assert args[1].endswith('scripts/install-target.sh')
            (appdir / 'marker').write_text('new')
            (appdir / '.venv/marker').write_text('new env')
        if args[0] == 'curl':
            return json.dumps({'service': 'netem-traffic-target', 'version': '0.1.0'})
        return ''
    with patch.object(host_admin, 'check_release', return_value={'available': True, 'tag': 'v99.0.0'}), \
         patch.object(host_admin, 'files_manifest', return_value={'marker': 'old'}), \
         patch.object(host_admin, 'run', side_effect=run):
        with pytest.raises(ValueError, match='previous installation restored'):
            host_admin.install_release('v99.0.0')
    assert (appdir / 'marker').read_text() == 'old'
    assert (appdir / '.venv/marker').read_text() == 'old env'
    assert ['systemctl', 'restart', 'netem-traffic-target'] in calls
    assert ['systemctl', 'restart', 'netem-traffic-target-manager'] in calls


def test_simulator_target_controls_require_admin_csrf_and_idle_install(simulator):
    client = simulator.test_client()
    assert client.post('/settings/system/action', data={'action': 'target_install'}).status_code == 400
    with client.session_transaction() as session:
        session['trafficgen_admin'] = True; session['csrf_token'] = 'token'
    with patch.object(maintenance, 'status', return_value={'busy': False}), \
         patch.object(simulator.config['TRAFFICGEN_CONTROLLER'], 'status', return_value={'status': 'running'}), \
         patch.object(maintenance, 'enqueue') as enqueue:
        client.post('/settings/system/action', data={'csrf_token': 'token', 'action': 'target_install', 'tag': 'v99.0.0'})
        enqueue.assert_not_called()
    with patch.object(maintenance, 'status', return_value={'busy': False}), \
         patch.object(simulator.config['TRAFFICGEN_CONTROLLER'], 'status', return_value={'status': 'stopped'}), \
         patch.object(maintenance, 'enqueue') as enqueue:
        client.post('/settings/system/action', data={'csrf_token': 'token', 'action': 'target_install', 'tag': 'v99.0.0'})
        enqueue.assert_called_once()
        assert enqueue.call_args.args[1:] == ('target_install', {'tag': 'v99.0.0'})
