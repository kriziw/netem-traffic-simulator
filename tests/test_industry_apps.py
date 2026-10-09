import json
import socket
import threading

import gevent
import pytest
from gevent.pywsgi import WSGIServer
from locust.env import Environment

from trafficgen.database import recent_runs
from trafficgen.engine import CorporateUser
from trafficgen.profiles import APPLICATIONS, MEDIA_MODES, PERSONAS, profile_payload
from trafficgen.target import app as target_app, udp_sink

INDUSTRY_APPS = ("ot_telemetry", "mes", "erp", "plm_cad", "pos", "wms_scan", "emr",
                 "pacs_imaging", "core_banking", "cctv_backhaul", "guest_internet")


def test_catalog_is_consistent():
    for name, app in APPLICATIONS.items():
        assert app["class"] in ("interactive", "bulk", "realtime"), name
        assert 0 < app["latency_good_ms"] < app["latency_poor_ms"], name
        assert callable(getattr(CorporateUser, f"_app_{name}", None)), f"{name} has no handler"
    for name, persona in PERSONAS.items():
        assert set(persona["applications"]) <= set(APPLICATIONS), name
    for mode in MEDIA_MODES.values():
        assert mode["tolerance_pct"]["ot_telemetry"] == 0.0
    assert set(INDUSTRY_APPS) <= set(profile_payload()["applications"])


@pytest.fixture
def target():
    server = WSGIServer(("127.0.0.1", 0), target_app, log=None)
    server.start()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    udp_port = probe.getsockname()[1]
    probe.close()
    stop = threading.Event()
    worker = gevent.spawn(udp_sink, "127.0.0.1", udp_port, stop)
    gevent.sleep(0.02)
    yield f"http://127.0.0.1:{server.server_port}", udp_port
    stop.set()
    worker.kill()
    server.stop()


@pytest.mark.parametrize("application", INDUSTRY_APPS)
def test_industry_apps_complete_against_the_target(target, application):
    host, udp_port = target
    environment = Environment()
    events = []
    environment.events.request.add_listener(lambda **event: events.append(event))
    config = dict(run_id="t", personas={"knowledge_worker": 100}, applications={application: 100},
                  target=host, udp_port=udp_port, media_mode="strict")
    user = type("IndustryUser", (CorporateUser,), {"abstract": False, "host": host, "runtime_config": config})(environment)
    user.on_start()
    getattr(user, f"_app_{application}")()
    assert events, application
    for event in events:
        assert event["exception"] is None, (application, event["exception"])
        assert event["context"]["application"] == application


def test_run_label_is_validated_and_kept(simulator):
    headers = {"Authorization": "Bearer " + simulator.config["TRAFFICGEN_SETTINGS"].api_key_path.read_text().strip()}
    client = simulator.test_client()
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]
    assert client.post("/api/v1/workloads/start", json={"label": "x" * 121}, headers=headers).status_code == 400
    assert client.post("/api/v1/workloads/start", json={"label": 5}, headers=headers).status_code == 400
    try:
        response = client.post("/api/v1/workloads/start", json={
            "users": 1, "target": "http://127.0.0.1:9", "label": "Automotive plant · Large · Mission-critical",
            "personas": {"shop_floor": 60, "ot_device": 40}, "applications": {app: 1 for app in APPLICATIONS}}, headers=headers)
        assert response.status_code == 201, response.json
        assert response.json["run"]["label"] == "Automotive plant · Large · Mission-critical"
        assert recent_runs(controller.settings.database_path)[0]["label"] == "Automotive plant · Large · Mission-critical"
        assert "Automotive plant" in simulator.test_client().get("/api/v1/status", headers=headers).get_data(as_text=True)
    finally:
        controller.stop()
