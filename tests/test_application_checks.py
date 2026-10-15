import time
from unittest.mock import Mock, patch

import gevent
import pytest

from trafficgen.application_checks import bound_request, enabled_applications, measurement_counts, packet_wait
from trafficgen.database import init_db, record_transactions, save_contract, record_operation_events
from trafficgen.dem import dem_summary
from trafficgen.engine import ApplicationCheckUser, CorporateUser
from trafficgen.experience_policy import validate_policy


def test_bounds_and_explicit_source_counts():
    args, kwargs = bound_request(('POST', '/files/upload?kb=8192&kind=check'), {'data': b'x' * 100000})
    assert args[1] == '/files/upload?kb=32&kind=check'
    assert len(kwargs['data']) == 32768 and kwargs['timeout'] == (2, 2)
    assert measurement_counts([{'endpoint_id': 'check-a', 'response_length': 32, 'bytes_up': 16},
                               {'endpoint_id': 'ep-a'}])['application_checks'] == 1
    assert enabled_applications({'application_checks': True, 'generation_mode': 'rate'}) == []


def test_delayed_sender_never_catches_up_monitoring_packets_and_pacing_is_not_network_delay():
    from trafficgen.operation import Operation
    # A sender stalled for half a second still spaces its next packet normally.
    assert packet_wait(.02, origin=10, sequence=2, now=10.5, monitoring=True) == .02
    assert packet_wait(.02, origin=10, sequence=2, now=10.5) == 0  # Existing user-mode behavior.
    events = []
    with patch('trafficgen.operation.time.monotonic', return_value=100) as clock, \
         patch('trafficgen.operation.gevent.spawn_later'):
        operation = Operation({'policy': {'voice': {'deadline_ms': 10}}},
                              {'application': 'voice'}, events.append, pacing_ms=100)
        clock.return_value = 100.03  # Three times the network target, within intentional pacing.
        operation.close()
    assert [event['kind'] for event in events] == ['started', 'completed']
    assert events[0]['intentional_pacing_ms'] == 100


def test_sender_lag_cannot_be_reported_as_bad_network_performance(tmp_path):
    db = tmp_path / 'lag.db'
    init_db(db)
    now = time.time()
    save_contract(db, {'run_id': 'lag', 'policy': validate_policy({}),
                       'manifest': {'application_shares': {'web_saas': 1}}}, now - 20)
    record_transactions(db, [dict(run_id='lag', timestamp=now-1, application='web_saas', endpoint_id='check-a',
                                 success=False, response_time_ms=1000, egress='192.0.2.1')] * 20)
    record_operation_events(db, [{'run_id': 'lag', 'operation_id': 'late', 'timestamp': now-1,
                                 'kind': 'application_check_generator_lag', 'application': 'web_saas'}])
    result = dem_summary(db, 'lag', since=now-10, until=now)
    assert result['experience_score'] is None and result['business_acceptance'] == 'unknown'
    assert result['evidence_issues'] == ['application_check_generator_lag']
    assert result['diagnosis']['findings'] == [] and result['diagnosis']['egress'] == {}


def test_checks_supply_application_evidence_without_inventing_employees_and_obey_phase_bounds(tmp_path):
    db = tmp_path / 'checks.db'
    init_db(db)
    now = time.time()
    apps = ['web_saas', 'erp', 'pos', 'dns']
    config = {'run_id': 'small', 'application_checks': True, 'policy': validate_policy({}),
              'manifest': {'application_shares': {app: .25 for app in apps}}}
    save_contract(db, config, now - 30)
    checks = [dict(run_id='small', timestamp=now-1, application=app, endpoint_id='check-' + app,
                   persona='application_monitor', success=True, response_time_ms=10, response_length=1024)
              for app in apps for _ in range(5)]
    # Previous-phase failures must not contaminate current-phase checks.
    record_transactions(db, [dict(checks[0], timestamp=now-20, success=False)] * 20 + checks +
                        [dict(checks[0], endpoint_id='ep-1', persona='knowledge_worker')])
    result = dem_summary(db, 'small', since=now-10, until=now)
    assert result['scoring_complete'] and result['business_acceptance'] == 'pass'
    assert result['measurement_sources']['application_checks'] == 20
    assert result['measurement_sources']['user_transactions'] == 1
    assert list(result['endpoints']) == ['ep-1']
    assert 'application_monitor' not in result['personas']
    assert all(item['measurement_sources']['application_checks'] == 5 for item in result['applications'].values())
    record_transactions(db, [dict(checks[0], timestamp=now-.5, success=False)] * 10)
    assert dem_summary(db, 'small', since=now-10, until=now)['business_acceptance'] == 'fail'


def test_worker_schedules_each_app_and_stops_with_run_without_adding_users(simulator):
    controller = simulator.config['TRAFFICGEN_CONTROLLER']
    events = []
    active = {'count': 0, 'maximum': 0}

    def observed(user):
        active['count'] += 1
        active['maximum'] = max(active['maximum'], active['count'])
        events.append(user._context('web_saas'))
        try:
            gevent.sleep(.06)  # Slower than the requested check interval.
        finally:
            active['count'] -= 1

    with patch.object(CorporateUser, 'tasks', [lambda self: gevent.sleep(.01)]), \
         patch.object(ApplicationCheckUser, '_app_web_saas', observed), \
         patch('trafficgen.application_checks.check_interval', return_value=.01):
        controller.start({'users': 2, 'spawn_rate': 100, 'target': 'http://127.0.0.1:9',
                          'applications': {'web_saas': 1}, 'application_checks': True})
        with gevent.Timeout(2):
            while len(events) < 5:
                gevent.sleep(.01)
        assert controller.runner.user_count == 2
        assert len(events) >= 5
        assert all(item['endpoint_id'].startswith('check-') for item in events)
        assert all(item['persona'] == 'application_monitor' for item in events)
        assert active['maximum'] == 1  # Slow checks never overlap or build a queue.
        controller.run['live_context']['until'] = time.time()
        count = len(events)
        gevent.sleep(.04)
        assert len(events) == count
        controller.stop()
        assert not controller.application_check_greenlets
        assert len(events) == count


def test_monitoring_validates_boolean_and_rejects_rate_calibration(simulator):
    controller = simulator.config['TRAFFICGEN_CONTROLLER']
    for raw in ({'application_checks': 'yes'}, {'application_checks': True, 'paced_mbps': 10}):
        with pytest.raises(ValueError):
            controller._validate_start(raw)


def test_failed_check_initialization_is_reported(simulator):
    from locust.env import Environment
    controller = simulator.config['TRAFFICGEN_CONTROLLER']
    run = {'run_id': 'r', 'target': 'http://127.0.0.1:9'}
    with patch.object(ApplicationCheckUser, 'on_start', side_effect=RuntimeError('source binding failed')):
        controller._check_application(run, Environment(), 'web_saas', 0)
    assert 'web_saas' in run['application_check_errors']


def test_monitoring_keeps_source_binding_and_actual_capped_object_metadata():
    from locust.env import Environment
    from trafficgen.traffic_paths import bind_http

    run = {'run_id': 'r', 'seed': 1, 'personas': {'knowledge_worker': 1}, 'applications': {'web_saas': 1},
           'target': 'http://127.0.0.1:9', 'udp_port': 9999, 'traffic_path': {'source': '127.0.0.2'}}
    user_class = type('Check', (ApplicationCheckUser,), {'host': run['target'], 'runtime_config': run})
    user = user_class(Environment())
    with patch('trafficgen.traffic_paths.bind_http', wraps=bind_http) as binding:
        user.on_start()
        binding.assert_called_once_with(user.client, '127.0.0.2')
    request = Mock(return_value=Mock(__enter__=Mock(return_value=Mock(status_code=200)), __exit__=Mock()))
    user._bounded_request(request)('GET', '/files/download?kb=8192', context={})
    context = request.call_args.kwargs['context']
    assert context['expected_bytes'] == 32768
    assert request.call_args.args[1] == '/files/download?kb=32'
    user.on_stop()


def test_two_user_local_target_smoke_populates_all_traffic_groups(simulator):
    """One real HTTP/UDP smoke; no WAN or appliance setup is changed."""
    from dataclasses import replace
    import threading
    from gevent.pywsgi import WSGIServer
    from trafficgen.target import app, udp_sink, udp_socket

    server = WSGIServer(('127.0.0.1', 0), app, log=None)
    server.start()
    sock = udp_socket('127.0.0.1', 0)
    stopped = threading.Event()
    echo = gevent.spawn(udp_sink, '127.0.0.1', sock.getsockname()[1], stopped, sock)
    controller = simulator.config['TRAFFICGEN_CONTROLLER']
    controller.settings = replace(controller.settings, target_udp_port=sock.getsockname()[1])
    try:
        # Idle employees isolate the extra checks' contribution to evidence.
        with patch.object(CorporateUser, 'tasks', [lambda self: gevent.sleep(.1)]):
            controller.start({'users': 2, 'spawn_rate': 100, 'target': f'http://127.0.0.1:{server.server_port}',
                              'applications': {'web_saas': 1, 'voice': 1, 'file_sync': 1}, 'application_checks': True})
            gevent.sleep(13.5)
            controller.flush_completed()
            summary = dem_summary(controller.settings.database_path, controller.run['run_id'], window_seconds=15)
            assert controller.runner.user_count == 2
            assert summary['scoring_complete'] and summary['business_acceptance'] == 'pass'
            assert summary['measurement_sources']['application_checks'] >= 20
            assert summary['measurement_sources']['user_transactions'] == 0
            assert summary['endpoints'] == {}
            assert {app['class'] for app in summary['applications'].values()} == {'interactive', 'realtime', 'bulk'}
            assert all(app['requests'] >= 5 for app in summary['applications'].values())
            assert all(app['experience_score'] is not None for app in summary['applications'].values())
            assert summary['applications']['voice']['quality']['udp_loss_pct'] == 0
            assert not controller.run.get('application_check_errors')
    finally:
        controller.stop()
        stopped.set()
        echo.kill()
        sock.close()
        server.stop()
