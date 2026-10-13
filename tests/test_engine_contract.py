import time
from unittest.mock import Mock

import gevent
import pytest

from trafficgen.database import init_db, record_transactions, save_contract, record_data_loss
from trafficgen.dem import summarize_rows, dem_summary
from trafficgen.dem_worker import fresh_snapshot
from trafficgen.experience_policy import validate_policy, save_policy
from trafficgen.rate import RateScheduler
from trafficgen.workload import compile_manifest, allocate_counts
from trafficgen.engine import CorporateUser


def row(app="web_saas", ms=297, ok=True, **extra):
    return dict(application=app, success=ok, response_time_ms=ms, response_length=100,
                timestamp=time.time() - 1, run_id="example", **extra)


def test_example_partial_voice_failure_is_not_a_site_outage():
    rows = [row()] * 108 + [row("voice", 100)] * 4 + [row("voice", 0, False)]
    result = summarize_rows(rows, application_shares={"web_saas": .95, "voice": .05})
    assert result["requests"] == 113
    assert result["availability_pct"] == pytest.approx(99.115)
    assert 98 < result["experience_score"] < 100
    assert result["critical_alerts"][0]["application"] == "voice"
    assert result["scoring_complete"]


def test_configured_target_is_authoritative_and_elastic():
    for ms in (299, 300, 301, 600):
        score = summarize_rows([row(ms=ms)] * 20, "web_saas")
        assert score["experience_score"] == pytest.approx(min(100, 30000 / ms), abs=.01)
    policy = validate_policy({"web_saas": {"deadline_ms": 2000}})
    assert summarize_rows([row(ms=1200)] * 20, "web_saas", policy)["experience_score"] == 100
    policy = validate_policy({"web_saas": {"hard_deadline": True}})
    assert summarize_rows([row(ms=301)] * 20, "web_saas", policy)["experience_score"] == 0


def test_missing_expected_application_is_unknown_not_renormalized():
    result = summarize_rows([row()] * 100, application_shares={"web_saas": .5, "erp": .5})
    assert not result["scoring_complete"]
    assert result["experience_score"] is None
    assert result["coverage_pct"] == 50
    assert "erp" in result["missing_measurements"]


def test_exact_half_open_window_and_immutable_run_policy(tmp_path):
    db = tmp_path / "traffic.db"
    init_db(db)
    now = time.time()
    policy = validate_policy({"web_saas": {"deadline_ms": 2000}})
    contract = {"run_id": "example", "policy": policy, "manifest": {"application_shares": {"web_saas": 1}}}
    save_contract(db, contract, now - 20)
    rows = [dict(row(ms=1200), timestamp=now - 3)] * 20
    rows += [dict(row(ok=False), timestamp=now - 1), dict(row(ok=False), timestamp=now + 10)]
    record_transactions(db, rows)
    save_policy(db, {"web_saas": {"deadline_ms": 1}})
    result = dem_summary(db, "example", since=now - 4.8, until=now - 1)
    assert result["window_seconds"] == pytest.approx(3.8)
    assert result["requests"] == 20
    assert result["experience_score"] == 100
    assert result["policy_source"] == "run_snapshot"
    assert result["scoring_complete"]
    record_data_loss(db, "example", 1)
    assert not dem_summary(db, "example", since=now - 4.8, until=now - 1)["scoring_complete"]


def test_row_cap_and_future_snapshot_cannot_claim_complete_evidence(tmp_path):
    db = tmp_path / "traffic.db"
    init_db(db)
    record_transactions(db, [row()] * 50000)
    result = dem_summary(db, "example", diagnosis=False)
    assert result["truncated"] and not result["scoring_complete"]
    assert fresh_snapshot({"run_id": "example", "created": 101, "dem": {"requests": 100}}, "example", now=100) is None


def test_sustained_application_budget_is_not_reapplied_per_endpoint():
    policy = validate_policy({"file_sync": {"min_sustained_down_mbps": 8}})
    rows = [dict(row("file_sync", ms=100), response_length=1000000)] * 6
    assert summarize_rows(rows, "file_sync", policy, scope="subset")["experience_score"] == 100
    assert summarize_rows(rows, "file_sync", policy)["experience_score"] == 10


def test_rate_arrivals_continue_when_workers_do_not_reply():
    scheduler = RateScheduler({"target_users": 1, "paced_mbps": 8})
    scheduler.offer(0)
    scheduler.offer(1)
    assert scheduler.offered_bytes == 4 * 256 * 1024
    assert scheduler.admitted_bytes == 256 * 1024
    assert scheduler.missed_requests == 3
    scheduler.config["target_users"] = 20
    scheduler.offer(2)
    assert scheduler.offered_bytes == 8 * 256 * 1024
    assert scheduler.snapshot()["target_mbps"] == 8


def test_population_allocation_matches_manifest():
    assert allocate_counts({"knowledge_worker": 7, "collaboration_user": 3, "background": 2, "camera": 4}, 16) == {
        "knowledge_worker": 7, "collaboration_user": 3, "background": 2, "camera": 4}
    manifest = compile_manifest({"personas": {"warehouse_operator": 1, "camera": 1}, "target_users": 2,
                                 "applications": {"erp": 1, "wms_scan": 1, "cctv_backhaul": 1}})
    assert manifest["application_shares"]["erp"] > 0
    assert sum(manifest["application_shares"].values()) == pytest.approx(1)


def test_cancelled_http_is_recorded_and_removed_from_outstanding_work():
    user = object.__new__(CorporateUser)
    user.runtime_config = {}
    user.environment = Mock()
    request = Mock(side_effect=gevent.GreenletExit)
    with pytest.raises(gevent.GreenletExit):
        user._bounded_request(request)("GET", "/", context={"application": "erp"})
    event = user.environment.events.request.fire.call_args.kwargs
    assert event["context"]["cancelled"]
    assert event["exception"] is not None
    assert not user.runtime_config["in_flight"]
