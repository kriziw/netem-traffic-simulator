import pytest
import time
import gevent
from unittest.mock import patch

from trafficgen.rate import RateScheduler
from trafficgen.dem import summarize_rows
from trafficgen.experience_policy import validate_policy
from trafficgen.database import init_db, record_operation_events, operation_evidence, record_transactions
from trafficgen.operation import Operation
from trafficgen.dem import dem_summary
from trafficgen.packet_metrics import histogram, merge, percentile


def test_lifecycle_persists_expiry_cancellation_and_clock_change(tmp_path):
    db = tmp_path / "events.db"
    init_db(db)
    events = []
    cfg = {"policy": {"voice": {"deadline_ms": 1}}}
    operation = Operation(cfg, {"run_id": "r", "application": "voice"}, events.append)
    gevent.sleep(.01)
    assert events[-1]["kind"] == "deadline_expired"
    with patch("trafficgen.operation.time.time", return_value=time.time() + 5):
        operation.close(cancelled=True)
    record_operation_events(db, events)
    evidence = operation_evidence(db, "r", events[0]["timestamp"] - 1, events[-1]["timestamp"] + 1)
    assert evidence["events"] == {"started": 1, "deadline_expired": 1, "cancelled": 1}
    assert evidence["clock_discontinuities"] == 1 and evidence["outstanding_at_end"] == 0


def test_packet_histogram_pools_packets_instead_of_burst_means():
    combined = merge([histogram([1] * 100), histogram([300] * 10)])
    assert percentile(combined, .95) == 300
    assert percentile(combined, .50) == 1


def test_active_phase_segments_exclude_paused_failures(tmp_path):
    db = tmp_path / "intervals.db"
    init_db(db)
    now = time.time()
    good = dict(run_id="r", application="web_saas", success=True, response_time_ms=10, response_length=100)
    record_transactions(db, [dict(good, timestamp=now - 9)] * 20 +
                        [dict(good, timestamp=now - 5, success=False)] * 100 +
                        [dict(good, timestamp=now - 2)] * 20)
    result = dem_summary(db, "r", since=now-10, until=now-1,
                         intervals=[[now-10, now-8], [now-3, now-1]])
    assert result["requests"] == 40 and result["experience_score"] == 100
    assert result["window_seconds"] == 4
    with pytest.raises(ValueError):
        dem_summary(db, "r", since=now-10, until=now-1, intervals=[[now-10, now-4], [now-5, now-1]])


def test_directional_rate_budgets_survive_population_changes():
    cfg = {"target_users": 100, "rate_down_mbps": 8, "rate_up_mbps": 4}
    scheduler = RateScheduler(cfg)
    for i in range(1001):
        scheduler.offer(i / 100)
        while not scheduler.jobs.empty():
            direction, kb, _ = scheduler.jobs.get()
            scheduler.dispatched(direction, kb)
            scheduler.completed(direction, kb * 1024)
            scheduler.jobs.task_done()
        if i == 500:
            cfg["target_users"] = 200
    for direction, target in (("down", 8), ("up", 4)):
        counts = scheduler.snapshot()["directions"][direction]
        assert counts["started_bytes"] * 8 / 10 / 1e6 == pytest.approx(target, abs=.22)
        assert counts["missed_requests"] == 0


def test_upload_only_configuration_and_ambiguous_rates(simulator):
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]
    run = controller._validate_start({"rate_up_mbps": 10, "users": 2})
    assert run["generation_mode"] == "rate" and run["rate_down_mbps"] == 0
    with pytest.raises(ValueError):
        controller._validate_start({"paced_mbps": 10, "rate_up_mbps": 10})


def test_failed_download_is_zero_transfer_evidence_not_missing():
    rows = [dict(application="file_sync", success=False, response_time_ms=1000,
                 response_length=0, transfer_direction="down", expected_bytes=262144)] * 20
    policy = validate_policy({"file_sync": {"min_down_mbps": 1}})
    summary = summarize_rows(rows, "file_sync", policy)
    assert summary["scoring_complete"]
    assert summary["quality"]["transfer_down_p05_mbps"] == 0
    assert summary["experience_score"] == 0


def test_throughput_scope_means_a_budget_per_endpoint():
    rows = [dict(application="file_sync", endpoint_id=endpoint, success=True, response_time_ms=100,
                 response_length=1000000) for endpoint in ("one", "two") for _ in range(10)]
    policy = validate_policy({"file_sync": {"sustained_scope": "endpoint", "min_sustained_down_mbps": 2}})
    result = summarize_rows(rows, "file_sync", policy, window_seconds=60)
    assert result["quality"]["scope_members"] == 2
    assert result["score_components"]["sustained_down"] == pytest.approx(200 / 3)


def test_aggregation_queue_is_bounded_and_deduplicates(tmp_path):
    from trafficgen.aggregation import enqueue, read_result, process_one
    db = tmp_path / "worker.db"
    init_db(db)
    key = enqueue(db, {"run_id": "same"})
    assert enqueue(db, {"run_id": "same"}) == key
    for index in range(7):
        enqueue(db, {"run_id": str(index)})
    with pytest.raises(ValueError, match="queue is full"):
        enqueue(db, {"run_id": "ninth"})
    assert process_one(str(db))  # The worker CLI supplies a string, unlike unit fixtures.
    assert read_result(db, key)["dem"]["requests"] == 0


def test_supervised_process_serves_queries(simulator, monkeypatch):
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]
    monkeypatch.delenv("TRAFFICGEN_DEM_WORKER", raising=False)
    controller.dem_worker = controller._start_dem_worker()
    result = controller.query_dem(run_id="worker-enabled-regression", window_seconds=60)
    assert result["requests"] == 0
    assert "aggregation_unavailable" not in result["evidence_issues"]
    assert result["interactive_p95_ms"] is None


def test_sustained_media_is_rejected_before_run_creation_on_old_target(simulator):
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]
    with patch.object(controller, "_probe_target", return_value={"capabilities": ["media-echo", "observed-source"]}):
        with pytest.raises(ValueError, match="update the target first"):
            controller.start({"media_pattern": "sustained"})
    assert controller.run is None


def test_full_media_echo_has_matching_payload_and_packet_histogram():
    import socket
    import threading
    from locust.env import Environment
    from trafficgen.engine import CorporateUser
    from trafficgen.target import udp_sink, udp_socket
    sock = udp_socket("127.0.0.1", 0)
    port = sock.getsockname()[1]
    stop = threading.Event()
    worker = gevent.spawn(udp_sink, "127.0.0.1", port, stop, sock)
    environment = Environment()
    events = []
    environment.events.request.add_listener(lambda **event: events.append(event))
    user_class = type("FullEchoUser", (CorporateUser,), {"abstract": False, "host": "http://127.0.0.1",
        "runtime_config": dict(run_id="test", personas={"knowledge_worker": 100}, applications={"voice": 100},
                               target="http://127.0.0.1", udp_port=port)})
    user = user_class(environment)
    user.on_start()
    try:
        user._udp_burst("voice", 160, 5, .02, full_echo=True)
        assert events[0]["response_length"] == 800
        assert sum(events[0]["context"]["packet_rtt_histogram"].values()) == 5
        assert events[0]["context"]["packets_late"] == 0
    finally:
        stop.set()
        worker.kill()
        sock.close()
