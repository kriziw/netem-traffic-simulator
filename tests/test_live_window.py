import time
from unittest.mock import patch

import pytest

from trafficgen import database, dem_worker, live_window
from trafficgen.dem import dem_summary


def make_run(tmp_path, window=15):
    path = tmp_path / 'traffic.db'
    database.init_db(path)
    now = time.time()
    run = dict(run_id='live-test', started_at=now-120, status='running', profile='office', target='http://t:8090',
               target_users=1, spawn_rate=1, activity='normal', personas={}, applications={}, media_mode='strict',
               live_window_seconds=window)
    database.create_run(path, run)
    return path, run, live_window.initialize(path, run), now


@pytest.mark.parametrize('value', [True, False, 4, 121, 15.5, 'NaN', None, 'bad'])
def test_invalid_live_window_is_rejected(value):
    with pytest.raises(ValueError):
        live_window.window_seconds(value)


def test_worker_default_custom_window_phase_cut_and_full_phase_report(tmp_path):
    path, run, context, now = make_run(tmp_path)
    for age in (80, 20, 8, 4):
        database.record_transaction(path, dict(run_id=run['run_id'], timestamp=now-age,
            application='web_saas', endpoint_id='web', success=True, response_time_ms=10))
    assert dem_worker.publish_once(path)['dem']['requests'] == 2  # Default 15 s.
    context = live_window.change_context(path, run['run_id'], context,
        {'phase_id': 'baseline', 'window_seconds': 60}, now=now-120)
    assert dem_worker.publish_once(path)['dem']['requests'] == 3
    old_snapshot = dem_worker.publish_once(path)
    context = live_window.change_context(path, run['run_id'], context,
        {'phase_id': 'degradation', 'window_seconds': 15}, now=now-6)
    current = dem_worker.publish_once(path)['dem']
    assert current['requests'] == 1 and current['since'] == now-6
    assert current['live_phase_id'] == 'degradation'
    assert current['configured_window_seconds'] == 15
    assert dem_worker.fresh_snapshot(old_snapshot, run['run_id'], live_revision=context['revision']) is None
    # Reporting keeps every completion inside the full phase, independent of live scope.
    full = dem_summary(path, run_id=run['run_id'], since=now-90, until=now)
    assert full['requests'] == 4
    assert full['window_seconds'] == 90
    assert 'live_phase_id' not in full


def test_pause_freezes_live_window_resume_starts_fresh_and_reset_restores_borrowed_setting(tmp_path):
    path, run, context, now = make_run(tmp_path, window=60)
    context = live_window.change_context(path, run['run_id'], context,
        {'phase_id': 'phase-a', 'window_seconds': 15}, now=now-10)
    unchanged = live_window.change_context(path, run['run_id'], context,
        {'phase_id': 'phase-a', 'window_seconds': 15}, now=now-8)
    assert unchanged == context  # Periodic heartbeat cannot move the boundary.
    paused = live_window.change_context(path, run['run_id'], context,
        {'phase_id': 'phase-a', 'paused': True}, now=now-5)
    assert live_window.query_arguments(paused, now=now)['until'] == now-5
    resumed = live_window.change_context(path, run['run_id'], paused, {'phase_id': 'phase-a'}, now=now-2)
    assert resumed['since'] == now-2 and resumed['until'] is None
    reset = live_window.change_context(path, run['run_id'], resumed, {'reset': True}, now=now)
    assert reset['window_seconds'] == 60 and reset['phase_id'] is None
    assert reset['since'] == run['started_at']


def test_validation_precedes_context_write_and_wrong_revision_never_uses_old_phase(tmp_path):
    path, run, context, now = make_run(tmp_path)
    for raw in ({'phase_id': 'x', 'window_seconds': 121}, {'phase_id': 'x', 'paused': 'yes'}, {'phase_id': ''}):
        with pytest.raises(ValueError):
            live_window.change_context(path, run['run_id'], context, raw)
        assert live_window.read_context(path, run['run_id']) == context


def test_start_validation_and_http_context_preserve_report_queries(simulator):
    from unittest.mock import Mock
    controller = simulator.config['TRAFFICGEN_CONTROLLER']
    run = controller._validate_start({'profile': 'office', 'users': 1, 'live_window_seconds': 60})
    assert run['live_window_seconds'] == 60
    assert controller._validate_start({'profile': 'office', 'users': 1})['live_window_seconds'] == 15
    path = controller.settings.database_path
    run['status'] = 'running'
    database.create_run(path, run)
    run['live_context'] = live_window.initialize(path, run)
    controller.run = run
    controller.runner = Mock(user_count=1)
    client = simulator.test_client()
    headers = {'Authorization': 'Bearer ' + simulator.config['TRAFFICGEN_SETTINGS'].api_key_path.read_text().strip()}
    response = client.post('/api/v1/controller/heartbeat', headers=headers, json={
        'run_id': run['run_id'], 'phase': 'Degradation',
        'live_context': {'phase_id': 'test-1:1', 'window_seconds': 5}})
    assert response.status_code == 200
    dem = response.json['dem']
    assert dem['configured_window_seconds'] == 5
    assert dem['live_phase_id'] == 'test-1:1'
    assert dem['collecting_evidence'] is True
    summary = client.get('/api/v1/dem/summary', headers=headers).json
    assert summary['live_phase_id'] == 'test-1:1'  # Default API uses the selected live scope.
    longer = client.get('/api/v1/dem/summary?window=60', headers=headers).json
    assert longer['live_phase_id'] == 'test-1:1'
    assert longer['since'] == dem['since']
    bad = client.post('/api/v1/controller/heartbeat', headers=headers, json={
        'run_id': run['run_id'], 'live_context': {'phase_id': 'bad', 'window_seconds': 121}})
    assert bad.status_code == 400
    assert controller.run['live_context']['phase_id'] == 'test-1:1'
    controller.runner = None
    run['status'] = 'stopped'


def test_same_tick_or_clock_reversal_never_reuses_previous_phase(tmp_path):
    path, run, context, now = make_run(tmp_path)
    for instant in (now, now-1):
        boundary = dict(context, since=now)
        with patch('trafficgen.live_window.time.time', return_value=instant):
            summary = live_window.measure(path, run['run_id'], boundary)
        assert summary['requests'] == 0 and summary['scoring_complete'] is False
        assert summary['collecting_evidence'] is True
        assert 'live_interval_unavailable' in summary['evidence_issues']
