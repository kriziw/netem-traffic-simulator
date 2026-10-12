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


def test_controller_views_require_bearer_and_fixed_paths(simulator):
    client = local_client(simulator)
    endpoint = '/api/v1/controller/view'
    assert client.post(endpoint, json={}).status_code == 401
    headers = auth(simulator)
    for path in ('/api/v1/workloads/start', '/static/../../config.py', '//evil.example/', '/login'):
        assert client.post(endpoint, headers=headers, json={'path': path}).status_code == 400
    assert client.post(endpoint, headers=headers, json={'path': '/', 'fields': {'x': {}}}).status_code == 400
    response = client.post(endpoint, headers=headers, json={'path': '/workloads', 'csrf_token': 'token'}).json
    assert response['status'] == 200
    assert 'action="/simulator/workloads/start"' in response['body']
    assert '/simulator/static/app.js' in response['body']
    assert 'Back to NetEm' in response['body']
    assert b'Start simulation' not in client.get('/').data  # Trusted context did not leak.


def test_controller_command_uses_existing_executor_and_preserves_validation(simulator):
    client = local_client(simulator)
    controller = simulator.config['TRAFFICGEN_CONTROLLER']
    with patch.object(controller, 'stop') as stop:
        result = client.post('/api/v1/controller/view', headers=auth(simulator), json={
            'path': '/workloads/stop', 'method': 'POST', 'csrf_token': 'token', 'fields': {'csrf_token': 'token'}}).json
    stop.assert_called_once()
    assert result['location'] == '/simulator/workloads'
    assert result['status'] == 302
    assert client.post('/workloads/stop', data={'csrf_token': 'token'}).status_code == 403


def test_controller_static_asset_and_key_rotation(simulator):
    client = local_client(simulator)
    headers = auth(simulator)
    asset = client.post('/api/v1/controller/view', headers=headers, json={'path': '/static/app.js'}).json
    assert asset['status'] == 200 and 'TrafficGenControllerPrefix' in asset['body']
    result = client.post('/api/v1/controller/view', headers=headers, json={'path': '/settings/api/rotate',
        'method': 'POST', 'csrf_token': 'token', 'fields': {'csrf_token': 'token'}}).json
    assert result['api_key'] != headers['Authorization'][7:]
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
    body = client.post('/api/v1/controller/view', headers=auth(simulator), json={'path': '/traffic-paths'}).json['body']
    assert 'Saved paths · not verified' in body
    assert 'SD-WAN appliance' in body and 'Verify / retry' in body
    assert '/simulator/settings/network-setup?interface=eth1' in body
