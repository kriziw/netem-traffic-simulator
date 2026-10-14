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
    assert result["experience_score"] == 100
    assert result["score_status"] == "provisional"
    assert result["score_range"] == {"minimum": 50, "maximum": 100}
    assert result["business_acceptance"] == "unknown"
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


def test_snapshot_from_before_adjustment_is_not_current_evidence():
    snapshot = {"run_id": "example", "created": 100, "dem": {"requests": 100, "contract_epoch": 50}}
    assert fresh_snapshot(snapshot, "example", now=101, contract_epoch=60) is None
    assert fresh_snapshot(snapshot, "example", now=101, contract_epoch=50) is snapshot


def test_media_blackhole_is_complete_zero_without_requiring_unmeasurable_jitter():
    rows = [row("voice", ok=False, packets_sent=50, packets_lost=50)] * 20
    result = summarize_rows(rows, "voice")
    assert result["experience_score"] == 0
    assert result["scoring_complete"]


def test_population_reconciliation_tolerates_users_whose_start_hook_has_not_run():
    from types import SimpleNamespace
    from trafficgen.engine import WorkloadController
    controller = WorkloadController.__new__(WorkloadController)
    pending = SimpleNamespace()
    started = SimpleNamespace(persona="knowledge_worker")
    controller.runner = SimpleNamespace(user_greenlets=[SimpleNamespace(args=[pending]), SimpleNamespace(args=[started])])
    controller.run = {"manifest": {"persona_counts": {"knowledge_worker": 2}}}
    controller._reconcile_population()
    assert controller.run["persona_claimed"] == {"knowledge_worker": 1}
    assert not started.sparse_user


def test_phase_persistence_barrier_finishes_while_new_traffic_continues(simulator):
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]
    stop = gevent.event.Event()
    def produce():
        while not stop.is_set():
            controller.transaction_queue.put(row())
            gevent.sleep(.001)
    producer = gevent.spawn(produce)
    try:
        gevent.sleep(.01)
        gevent.with_timeout(3, controller.flush_completed)
        assert not producer.dead
        assert dem_summary(controller.settings.database_path, "example", diagnosis=False)["requests"] > 0
    finally:
        stop.set()
        producer.join()


def test_statistical_compliance_distinguishes_observations_and_confirmed_failures():
    policy = validate_policy({"erp": {"deadline_ms": 400, "success_min_pct": 99}})
    isolated = summarize_rows([row("erp", ms=100)] * 49 + [row("erp", ms=500)], "erp", policy)
    assert isolated["compliance"]["deadline"] is True  # Configured P95, not maximum.
    failure = summarize_rows([row("erp", ms=100)] * 49 + [row("erp", ok=False)], "erp", policy)
    assert failure["target_observations"] == ["success"]
    assert failure["target_violations"] == []
    assert failure["alert_severity"] == "observation"
    sustained = summarize_rows([row("erp", ms=100)] * 40 + [row("erp", ms=500)] * 10, "erp", policy)
    assert sustained["target_violations"] == ["deadline"]
    assert sustained["alert_severity"] == "violation"
    hard = validate_policy({"erp": {"deadline_ms": 400, "hard_deadline": True}})
    assert summarize_rows([row("erp", ms=100)] * 49 + [row("erp", ms=500)], "erp", hard)["target_violations"] == ["deadline"]


def test_confirmed_critical_outage_survives_missing_other_application():
    result = summarize_rows([row("erp", ok=False)] * 5, application_shares={"erp": .1, "web_saas": .9})
    assert result["experience_score"] == 0  # Only the observed application is down.
    assert result["score_range"] == {"minimum": 0, "maximum": 90}
    assert result["coverage_pct"] == 10
    assert not result["scoring_complete"]
    assert result["business_acceptance"] == "fail"
    assert result["critical_alerts"][0]["severity"] == "outage"


def test_legacy_shadow_does_not_control_primary_score():
    from trafficgen.legacy_score import legacy_summary
    rows = [row()] * 108 + [row("voice", 100)] * 4 + [row("voice", 0, False)]
    assert legacy_summary(rows)["experience_score"] == 0
    assert summarize_rows(rows, application_shares={"web_saas": .95, "voice": .05})["experience_score"] > 90


def test_manifest_warns_when_default_window_cannot_observe_sparse_application():
    manifest = compile_manifest({"personas": {"knowledge_worker": 1}, "target_users": 1,
                                 "applications": {"web_saas": 100, "erp": .001}, "activity": "light"})
    # ERP is absent in this persona; sampling estimates must reflect executable apps only.
    for key, guidance in manifest["sampling_guidance"].items():
        assert key in manifest["application_shares"]
        assert guidance["minimum_mean_window_s"] > 0
    assert manifest["sampling_guidance"]["web_saas"]["sparse_in_60s"]


def test_single_worker_directional_admission_cannot_starve_uploads():
    scheduler = RateScheduler({"target_users": 1, "rate_down_mbps": 4.194304, "rate_up_mbps": 2.097152})
    admitted = {"down": 0, "up": 0}
    for tick in range(20):
        scheduler.offer(tick / 2)
        direction, _, _ = scheduler.jobs.get_nowait()
        admitted[direction] += 1
        scheduler.jobs.task_done()
    assert admitted["up"] >= 8
    assert admitted["down"] >= 8
    assert scheduler.snapshot()["directions"]["up"]["offered_bytes"] == 10 * 256 * 1024


def test_idle_single_worker_admits_both_simultaneous_directions():
    scheduler = RateScheduler({"target_users": 1, "rate_down_mbps": 4.194304, "rate_up_mbps": 2.097152})
    admitted = {"down": 0, "up": 0}
    for tick in range(20):
        scheduler.offer(tick / 2)
        while not scheduler.jobs.empty():
            direction, _, _ = scheduler.jobs.get_nowait()
            admitted[direction] += 1
            scheduler.jobs.task_done()
    assert admitted == {"down": 20, "up": 10}
    assert scheduler.missed_requests == 0


def test_lost_measurements_cannot_confirm_a_statistical_business_failure(tmp_path):
    db = tmp_path / "lost.db"
    init_db(db)
    policy = validate_policy({})
    now = time.time()
    save_contract(db, {"run_id": "example", "policy": policy, "manifest": {"application_shares": {"erp": 1}}}, now - 10)
    record_transactions(db, [dict(row("erp", ok=False), timestamp=now - 1)] * 20)
    assert dem_summary(db, "example", diagnosis=False)["business_acceptance"] == "fail"
    record_data_loss(db, "example", 100)
    result = dem_summary(db, "example", diagnosis=False)
    assert result["business_acceptance"] == "unknown"
    assert result["critical_alerts"] == []
