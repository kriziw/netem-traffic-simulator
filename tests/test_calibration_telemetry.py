"""The figures the calibration log reads from the simulator: cheap, cumulative, and honest about a missing target."""
import gevent

from trafficgen import engine


def test_requests_failures_and_bytes_are_counted(simulator):
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]
    before = dict(controller.counters)
    controller._record_request("HTTP", "a", 12.0, 500, None, {})
    controller._record_request("HTTP", "b", 30.0, 0, RuntimeError("timeout"), {})
    assert controller.counters["requests"] == before["requests"] + 2
    assert controller.counters["failures"] == before["failures"] + 1
    assert controller.counters["bytes"] == before["bytes"] + 500


def test_paced_downloads_completed_are_counted_separately(simulator):
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]
    before = controller.counters["paced_completed"]
    controller._record_request("HTTP", "paced/bulk-download", 40.0, 4096, None, {})
    controller._record_request("HTTP", "office/mail", 12.0, 100, None, {})
    assert controller.counters["paced_completed"] == before + 1


def test_health_reports_counters_loop_samples_and_target_probe(simulator):
    response = simulator.test_client().get("/api/v1/health").json
    assert {"requests", "failures", "bytes", "paced_started", "paced_completed", "status_ms", "dem_ms",
            "write_ms"} <= set(response["counters"])
    assert response["loop"]["interval_s"] == engine.LOOP_INTERVAL_S
    assert isinstance(response["loop"]["samples"], list)
    assert isinstance(response["target_probe"]["samples"], list)


def test_the_health_reports_cpu_and_paced_timings(simulator):
    response = simulator.test_client().get("/api/v1/health").json
    assert {"cpu_s", "paced_wait_ms", "paced_transfer_ms"} <= set(response["counters"])
    assert response["counters"]["cpu_s"] >= 0


def test_loop_monitor_records_how_late_the_loop_woke(simulator):
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]
    gevent.sleep(0.6)
    assert len(controller.loop_samples) >= 1
    assert all(lag >= 0 for _, lag in controller.loop_samples)


def test_target_latency_is_none_when_the_target_does_not_answer(simulator, monkeypatch):
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]

    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(engine.requests, "get", refuse)
    assert controller._target_latency({"target": "https://192.0.2.1:8090"}) is None


def test_target_latency_is_measured_when_the_target_answers(simulator, monkeypatch):
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]

    class Answer:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(engine.requests, "get", lambda *args, **kwargs: Answer())
    latency = controller._target_latency({"target": "https://192.0.2.1:8090"})
    assert latency is not None and latency >= 0
