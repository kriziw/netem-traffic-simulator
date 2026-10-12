from unittest.mock import patch


def local_client(simulator):
    hook = simulator.before_request_funcs[None].pop(0)  # Exercise the production boundary.
    assert hook.__name__ == 'delegated_operator_test'
    client = simulator.test_client()
    with client.session_transaction() as state:
        state['trafficgen_admin'] = True
        state['csrf_token'] = 'token'
    return client


def auth(simulator):
    return {'Authorization': 'Bearer ' + simulator.config['TRAFFICGEN_SETTINGS'].api_key_path.read_text().strip()}


def test_local_ui_observes_but_cannot_issue_any_operator_command(simulator):
    client = local_client(simulator)
    page = client.get('/')
    assert b'Read-only execution monitor' in page.data
    assert b'Start simulation' not in page.data
    for path in ('/workloads', '/settings/network-setup', '/settings/system', '/settings/api', '/traffic-paths'):
        assert client.get(path).status_code == 302
    for path in ('/workloads/start', '/workloads/stop', '/workloads/adjust', '/experience/settings',
                 '/settings/network-setup', '/settings/system/action', '/settings/api/rotate'):
        assert client.post(path, data={'csrf_token': 'token'}).status_code == 403
    assert b'Business experience targets' not in client.get('/dem').data


def test_html_relay_is_removed_and_commands_require_bearer(simulator):
    client = local_client(simulator)
    assert client.post('/api/v1/controller/view', headers=auth(simulator), json={}).status_code == 404
    assert client.get('/api/v1/controller/settings').status_code == 401
    assert client.post('/api/v1/controller/commands/system', json={'action': 'scan'}).status_code == 401
    assert client.post('/api/v1/controller/commands/shell', headers=auth(simulator), json={}).status_code == 400
    assert client.post('/api/v1/controller/commands/system', headers=auth(simulator), json={'action': {}}).status_code == 400


def test_json_experience_commands_validate_and_are_idle_only(simulator):
    client = local_client(simulator)
    endpoint = '/api/v1/controller/commands/experience'
    headers = auth(simulator)
    assert client.post(endpoint, headers=headers, json={'voice': {'critical': True, 'deadline_ms': 200}}).status_code == 200
    assert client.post(endpoint, headers=headers, json={'voice': {'deadline_ms': 0}}).status_code == 409
    with patch.object(simulator.config['TRAFFICGEN_CONTROLLER'], 'status', return_value={'status': 'running'}):
        assert client.post(endpoint, headers=headers, json={}).status_code == 409
    assert client.post('/experience/settings', data={'csrf_token': 'token'}).status_code == 403


def test_json_key_rotation_updates_authentication_without_html(simulator):
    client = local_client(simulator)
    headers = auth(simulator)
    result = client.post('/api/v1/controller/commands/rotate-key', headers=headers, json={})
    assert result.status_code == 200 and result.json['api_key'] != headers['Authorization'][7:]
    assert client.get('/api/v1/status', headers=headers).status_code == 401
    assert client.get('/api/v1/status', headers=auth(simulator)).status_code == 200


def test_controller_lease_stops_only_its_run_and_heartbeat_tracks_phase(simulator):
    import time
    from unittest.mock import Mock
    controller = simulator.config['TRAFFICGEN_CONTROLLER']
    controller.run = {'run_id': 'owned', 'status': 'running', 'media_mode': 'strict'}
    controller.runner = Mock(user_count=5)
    with patch('trafficgen.engine.gevent.spawn'):
        controller.bind_controller_lease('owned', 30)
    client = simulator.test_client()
    result = client.post('/api/v1/controller/heartbeat', headers=auth(simulator), json={'run_id': 'owned', 'phase': 'Degradation'})
    assert result.status_code == 200 and result.json['run']['controller_phase'] == 'Degradation'
    assert client.post('/api/v1/controller/heartbeat', headers=auth(simulator), json={'run_id': 'other'}).status_code == 409
    controller.stop(expected_run_id='other')
    assert controller.run['status'] == 'running'
    controller.controller_lease['deadline'] = time.monotonic() - 1
    controller._watch_controller_lease('owned')
    assert controller.run['status'] == 'stopped'
    assert 'heartbeat expired' in controller.run['stop_reason']


def test_controller_retains_unverified_route_in_operator_inventory(simulator):
    import json
    from trafficgen import network_setup
    row = {'id': 'a' * 32, 'role': 'appliance', 'name': 'SD-WAN appliance', 'source': '10.250.3.100',
           'interface': 'eth1', 'gateway': '10.250.3.1', 'target': '198.18.0.1', 'mode': 'appliance', 'wan_id': ''}
    settings = simulator.config['TRAFFICGEN_SETTINGS']
    (settings.runtime_dir / network_setup.FILE).write_text(json.dumps({'appliance': row}))
    client = local_client(simulator)
    with patch('trafficgen.controller_api.interfaces', return_value=[]):
        reply = client.get('/api/v1/controller/settings', headers=auth(simulator))
    assert reply.status_code == 200
    cards = reply.json['network']['cards']
    draft = next(card for card in cards if card['role'] == 'appliance')
    assert draft['name'] == 'SD-WAN appliance' and draft['path'] is None
    assert 'body' not in reply.json and 'location' not in reply.json
