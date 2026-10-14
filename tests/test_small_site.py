import random
import time
from collections import Counter

import pytest

from trafficgen import database
from trafficgen.dem import summarize_rows, timing_measurements
from trafficgen.evidence_export import evidence_page
from trafficgen.workload import balanced_application


def test_balancing_preserves_shares_across_users_and_revisions_without_extra_actions():
    config, rng = {}, random.Random(12)
    weights = {'web_saas': 7, 'voice': 2, 'erp': 1, 'backup': 0}
    chosen = [balanced_application(config, 'worker', weights, rng) for _ in range(100)]
    assert Counter(chosen) == {'web_saas': 70, 'voice': 20, 'erp': 10}
    assert len(chosen) == 100
    # Shared ledger, separate persona and reset on a changed application mix.
    assert balanced_application(config, 'worker', {'erp': 1}, rng) == 'erp'
    assert balanced_application(config, 'guest', {'web_saas': 1}, rng) == 'web_saas'


def test_small_samples_have_measured_scores_but_cannot_pass_and_absence_is_unknown():
    rows = [dict(application='web_saas', success=True, response_time_ms=600,
                 response_length=100, timestamp=10)]
    app = summarize_rows(rows, 'web_saas')
    assert app['experience_score'] == 50
    assert app['sample_confidence'] == 'limited_sample'
    assert not app['scoring_complete']
    overall = summarize_rows(rows, application_shares={'web_saas': .25, 'erp': .75})
    assert overall['experience_score'] == 50
    assert overall['coverage_pct'] == 25
    assert overall['evidence_coverage_pct'] == 0
    assert overall['score_range'] == {'minimum': 12.5, 'maximum': 87.5}
    assert overall['business_acceptance'] == 'unknown'
    assert summarize_rows([], 'erp')['experience_score'] is None


def test_rtd_is_packet_distribution_not_application_completion_or_half_rtt():
    values = timing_measurements([
        {'request_type': 'HTTP', 'success': True, 'response_time_ms': 73, 'wait_ms': 8, 'transfer_ms': 65},
        {'request_type': 'UDP', 'success': True, 'response_time_ms': 999,
         'packet_rtt_histogram': {'1.0': 90, '8.0': 10}},
    ])
    assert values['application_completion_p95_ms'] == 73
    assert values['request_to_headers_p95_ms'] == 8
    assert values['response_body_p95_ms'] == 65
    assert values['packet_rtd_p95_upper_ms'] == 8
    assert values['packet_rtd_samples'] == 100
    assert values['packet_rtd_operations'] == 1
    assert values['one_way_latency_ms'] is None
    assert timing_measurements([])['packet_rtd_p95_upper_ms'] is None


def test_export_pagination_run_isolation_bounds_and_frozen_ceiling(tmp_path):
    path = tmp_path / 'traffic.db'
    database.init_db(path)
    now = time.time()
    run = dict(run_id='r', started_at=now-30, status='running', profile='office', target='http://t',
               target_users=1, spawn_rate=1, activity='normal', personas={}, applications={})
    database.create_run(path, run)
    for index in range(4):
        database.record_transaction(path, dict(run_id='r', timestamp=now-10+index, success=True, application='web_saas'))
    database.record_transaction(path, dict(run_id='other', timestamp=now-1, success=True))
    first = evidence_page(path, 'r', now-20, now, limit=2)
    assert len(first['items']) == 2 and not first['complete']
    database.record_transaction(path, dict(run_id='r', timestamp=now-1, success=True))
    second = evidence_page(path, 'r', now-20, now, cursor=first['next_cursor'], ceiling=first['ceiling'], limit=2)
    assert len(second['items']) == 2 and second['complete']
    assert all(item['run_id'] == 'r' for item in first['items'] + second['items'])
    with pytest.raises(ValueError):
        evidence_page(path, 'r', float('nan'), now)
    with pytest.raises(LookupError):
        evidence_page(path, 'missing', now-20, now)


def test_export_route_requires_auth_and_reports_invalid_queries(simulator):
    client = simulator.test_client()
    assert client.get('/api/v1/runs/r/evidence?since=1&until=2').status_code == 401
    key = simulator.config['TRAFFICGEN_SETTINGS'].api_key_path.read_text().strip()
    headers = {'Authorization': 'Bearer ' + key}
    assert client.get('/api/v1/runs/r/evidence?since=nan&until=2', headers=headers).status_code == 400
    assert client.get('/api/v1/runs/r/evidence?since=1&until=2', headers=headers).status_code == 404


def test_target_probe_uses_controlled_agent_and_never_adds_application_samples(tmp_path):
    import gevent
    from gevent.event import Event
    from trafficgen.target import udp_socket, udp_sink
    from trafficgen.target_timing import probe
    from trafficgen.dem import dem_summary
    sock = udp_socket('127.0.0.1', 0)
    stop = Event()
    worker = gevent.spawn(udp_sink, '127.0.0.1', sock.getsockname()[1], stop, sock)
    try:
        result = probe({'target': 'http://127.0.0.1:8090', 'udp_port': sock.getsockname()[1],
                        'traffic_path': {'source': '127.0.0.1'}})
        assert result['success']
        assert result['target_address'] == '127.0.0.1'
        assert result['source'] == '127.0.0.1'
        assert result['rtd_ms'] >= 0
        path = tmp_path / 'evidence.db'
        database.init_db(path)
        now = time.time()
        database.record_operation_events(path, [dict(result, kind='target_timing', operation_id='p', run_id='r', timestamp=now-.1)])
        summary = dem_summary(path, 'r', since=now-15, until=now)
        assert summary['requests'] == 0 and summary['experience_score'] is None
        assert summary['timing']['target_rtd']['samples'] == 1
        assert summary['timing']['target_rtd']['p95_ms'] == result['rtd_ms']
        assert not summary['scoring_complete']
    finally:
        stop.set()
        worker.join(timeout=2)
        worker.kill()


def test_pending_bulk_does_not_invalidate_unrelated_application(tmp_path):
    from trafficgen.dem import dem_summary
    path = tmp_path / 'evidence.db'
    database.init_db(path)
    now = time.time()
    database.record_transactions(path, [dict(timestamp=now-1, run_id='r', application='web_saas',
        success=True, response_time_ms=10)] * 20)
    database.record_operation_events(path, [dict(run_id='r', operation_id='pending', timestamp=now-2,
        application='backup', kind='deadline_expired')])
    result = dem_summary(path, 'r', since=now-15, until=now)
    assert result['at_risk'] and not result['scoring_complete']
    assert result['applications']['web_saas']['scoring_complete']
    assert result['operation_lifecycle']['overdue_by_application'] == {'backup': 1}
