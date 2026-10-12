from pathlib import Path
import time

from trafficgen.database import init_db, record_transaction
from trafficgen.dem import dem_summary, latency_score, reliability_score, summarize_rows


def test_dem_summary_tracks_endpoint_persona_and_application(tmp_path: Path):
    db = tmp_path / "test.db"
    init_db(db)
    now = time.time()
    rows = [
        {
            "timestamp": now,
            "run_id": "run-1",
            "endpoint_id": "ep-1",
            "persona": "knowledge_worker",
            "application": "web_saas",
            "request_type": "HTTP",
            "name": "web/page",
            "success": True,
            "response_time_ms": 100,
            "response_length": 1000,
        },
        {
            "timestamp": now - 1,
            "run_id": "run-1",
            "endpoint_id": "ep-1",
            "persona": "knowledge_worker",
            "application": "web_saas",
            "request_type": "HTTP",
            "name": "web/page",
            "success": False,
            "response_time_ms": 800,
            "response_length": 0,
            "error": "timeout",
        },
    ]
    for row in rows:
        record_transaction(db, row)

    summary = dem_summary(db, run_id="run-1", window_seconds=60)
    assert summary["requests"] == 2
    assert "web_saas" in summary["applications"]
    assert "knowledge_worker" in summary["personas"]
    assert "ep-1" in summary["endpoints"]
    assert summary["endpoints"]["ep-1"]["persona"] == "knowledge_worker"
    assert summary["experience_score"] == 0
    assert summary["applications"]["web_saas"]["experience_score"] == 0
    assert summary["personas"]["knowledge_worker"]["experience_score"] == 0
    assert summary["endpoints"]["ep-1"]["experience_score"] == 0


def transaction(application, latency=100, success=True):
    return dict(application=application, success=success,
                response_time_ms=latency, response_length=100)


def test_business_application_cannot_be_hidden_by_healthy_bulk_traffic():
    rows = [transaction("file_sync")] * 1000 + [transaction("web_saas", 1184)] * 100
    score = summarize_rows(rows)
    assert score["availability_pct"] == 100
    assert score["experience"] == "Critical"
    assert score["limiting_application"] == "web_saas"
    assert score["experience_score"] == summarize_rows(rows[-100:], "web_saas")["experience_score"]


def test_failed_only_application_sets_overall_score_to_zero():
    rows = [transaction("web_saas")] * 1000 + [transaction("erp", success=False)]
    assert summarize_rows(rows)["experience_score"] == 0
    assert summarize_rows(rows)["experience"] == "Critical"


def test_reliability_penalizes_failed_work_even_with_fast_responses():
    rows = [transaction("erp")] * 95 + [transaction("erp", success=False)] * 5
    assert summarize_rows(rows)["experience_score"] == 55
    assert reliability_score(90) == 0
    assert reliability_score(99) == 100


def test_latency_thresholds_preserve_application_tolerances():
    assert latency_score(300, "web_saas") == 100
    assert latency_score(1200, "web_saas") == 35
    assert latency_score(2400, "web_saas") == 0
    assert latency_score(1200, "updates") == 100
    assert summarize_rows([transaction("web_saas")])["experience_score"] == 100
    assert summarize_rows([])["experience_score"] is None


def test_deadline_breach_is_unusable_without_faking_a_transport_failure():
    result = summarize_rows([transaction("erp", 450)], "erp")
    assert result["availability_pct"] == 100
    assert result["quality"]["deadline_misses"] == 1
    assert result["quality"]["on_time_pct"] == 0
    assert result["score_components"]["deadline"] == 0


def test_noncritical_failures_do_not_limit_the_numeric_score():
    from trafficgen.experience_policy import default_policy
    policy = default_policy()
    rows = [transaction("web_saas")] + [transaction("updates", success=False)] * 100
    assert summarize_rows(rows, policy=policy)["experience_score"] == 100
    policy["updates"]["critical"] = True
    assert summarize_rows(rows, policy=policy)["experience_score"] == 0
    assert summarize_rows([transaction("updates")])["experience_score"] is None


def test_udp_loss_and_variation_have_independent_scores_and_coverage():
    row = dict(transaction("voice", 30), packets_sent=50, packets_lost=0, udp_jitter_ms=45)
    result = summarize_rows([row], "voice")
    assert result["score_components"]["udp_loss"] == 100
    assert result["score_components"]["udp_rtt_variation"] == 0
    row["udp_jitter_ms"] = 5
    row["packets_lost"] = 3
    assert summarize_rows([row], "voice")["score_components"]["udp_loss"] == 0
    row["packets_lost"] = 0
    row["udp_jitter_ms"] = None
    result = summarize_rows([row], "voice")
    assert result["scoring_complete"] is False
    assert "udp_rtt_variation" in result["missing_measurements"]


def test_transfer_speed_only_scores_real_transfers_with_explicit_targets():
    from trafficgen.experience_policy import default_policy
    policy = default_policy()
    policy["updates"].update(critical=True, min_down_mbps=10)
    row = dict(transaction("updates", 1000), response_length=1024 * 1024)
    result = summarize_rows([row], "updates", policy)
    assert result["score_components"]["throughput_down"] == 0
    assert result["quality"]["transfer_down_p05_mbps"] == 8.389
    row["response_length"] = 1024
    result = summarize_rows([row], "updates", policy)
    assert "throughput_down" not in result["score_components"]
    assert result["scoring_complete"] is False


def test_policy_persists_and_rejects_invalid_values(tmp_path):
    import pytest
    from trafficgen.experience_policy import load_policy, save_policy, validate_policy
    db = tmp_path / "traffic.db"
    saved = save_policy(db, {"updates": {"critical": True, "min_down_mbps": 10}})
    assert load_policy(db) == saved
    for raw in ({"unknown": {}}, {"erp": {"deadline_ms": float("nan")}},
                {"erp": {"deadline_ms": 0}}, {"voice": {"loss_max_pct": 101}},
                {"erp": {"critical": "true"}}):
        with pytest.raises(ValueError):
            validate_policy(raw)


def test_sustained_goodput_includes_the_entire_window():
    from trafficgen.experience_policy import default_policy
    policy = default_policy()
    policy["updates"].update(critical=True, min_sustained_down_mbps=1)
    rows = [dict(transaction("updates", 100), response_length=1024 * 1024)] * 4
    result = summarize_rows(rows, "updates", policy, window_seconds=60)
    assert result["quality"]["sustained_down_mbps"] == .559
    assert result["experience_score"] < 75
    assert summarize_rows(rows, "updates", policy, window_seconds=10)["score_components"]["sustained_down"] == 100


def test_admin_can_save_targets_but_cannot_change_them_during_a_run(simulator, monkeypatch):
    from trafficgen.experience_policy import default_policy, load_policy
    client = simulator.test_client()
    assert client.post('/experience/settings').status_code in (302, 400)
    with client.session_transaction() as state:
        state['trafficgen_admin'] = True
        state['csrf_token'] = 'token'
    fields = {'csrf_token': 'token'}
    for name, targets in default_policy().items():
        for key, value in targets.items():
            if key != 'critical' or value:
                fields[name + '.' + key] = 'on' if key == 'critical' else str(value)
    fields['updates.critical'] = 'on'
    fields['updates.min_down_mbps'] = '10'
    assert client.post('/experience/settings', data={**fields, 'csrf_token': 'wrong'}).status_code == 400
    assert client.post('/experience/settings', data=fields).status_code == 302
    db = simulator.config['TRAFFICGEN_SETTINGS'].database_path
    assert load_policy(db)['updates']['min_down_mbps'] == 10
    assert client.get('/dem').status_code == 200
    controller = simulator.config['TRAFFICGEN_CONTROLLER']
    monkeypatch.setattr(controller, 'status', lambda: {'status': 'running'})
    fields['updates.min_down_mbps'] = '20'
    assert client.post('/experience/settings', data=fields).status_code == 302
    assert load_policy(db)['updates']['min_down_mbps'] == 10
