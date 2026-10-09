import copy
import json
import socket
import sqlite3
import threading
import time
from pathlib import Path

import gevent
import pytest
from gevent.pywsgi import WSGIServer
from locust.env import Environment

from trafficgen.database import init_db, query_transactions, record_transaction, recent_runs, query_dem_timeseries, record_dem_sample
from trafficgen.dem import dem_summary, summarize_rows
from trafficgen.engine import CorporateUser, combined_application_weights
from trafficgen.profiles import normalized_mix, APPLICATIONS, PERSONAS
from trafficgen.target import app as target_app, udp_sink


def authorization(app):
    return {"Authorization": "Bearer " + app.config["TRAFFICGEN_SETTINGS"].api_key_path.read_text().strip()}


@pytest.mark.parametrize("payload", [[], [1], None, "text", 42])
def test_api_requires_object(simulator, payload):
    response = simulator.test_client().post("/api/v1/workloads/start", data=json.dumps(payload), content_type="application/json", headers=authorization(simulator))
    assert response.status_code == 400
    assert simulator.config["TRAFFICGEN_CONTROLLER"].run is None


@pytest.mark.parametrize("payload", [{"users": None}, {"users": 1.5}, {"users": True}, {"users": 0}, {"spawn_rate": "nan"}, {"spawn_rate": "inf"}, {"personas": []}, {"applications": {}}, {"applications": {"unknown": 1}}, {"applications": {"web_saas": -1}}, {"target": "http://127.0.0.1:bad"}, {"target": "http://127.0.0.1:0"}, {"target": "http://user:password@127.0.0.1"}, {"target": "http://127.0.0.1/path"}, {"target": "http://8.8.8.8"}])
def test_invalid_configuration_is_400_without_start(simulator, payload):
    response = simulator.test_client().post("/api/v1/workloads/start", json=payload, headers=authorization(simulator))
    assert response.status_code == 400
    assert simulator.config["TRAFFICGEN_CONTROLLER"].run is None


def test_workload_lifecycle_and_atomic_adjustment(simulator):
    server = WSGIServer(("127.0.0.1", 0), target_app, log=None)
    server.start()
    client = simulator.test_client()
    headers = authorization(simulator)
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]
    try:
        response = client.post("/api/v1/workloads/start", json={"users": 1, "spawn_rate": 100, "activity": "peak", "target": f"http://127.0.0.1:{server.server_port}", "applications": {"web_saas": 1}}, headers=headers)
        assert response.status_code == 201
        deadline = time.monotonic() + 5
        while controller.status()["dem"]["requests"] == 0 and time.monotonic() < deadline:
            gevent.sleep(0.05)
        assert controller.status()["dem"]["requests"] > 0
        assert client.post("/api/v1/workloads/start", json={}, headers=headers).status_code == 409
        previous = copy.deepcopy(controller.run)
        response = client.post("/api/v1/workloads/adjust", json={"users": 2, "applications": {"web_saas": 0}}, headers=headers)
        assert response.status_code == 400
        assert controller.run == previous
        response = client.post("/api/v1/workloads/adjust", json={"users": 2, "applications": {"dns": 1}, "personas": {"developer": 1}}, headers=headers)
        assert response.status_code == 200
        gevent.sleep(3)
        assert controller.status()["dem"]["applications"]["dns"]["requests"] > 0
        assert recent_runs(controller.settings.database_path)[0]["target_users"] == 2
        response = client.get("/api/v1/dem/experience", headers=headers)
        assert response.status_code == 200
        assert response.json["endpoint_experience"]["score"] is not None
        assert response.json["endpoints"]
        assert client.post("/api/v1/workloads/stop", headers=headers).status_code == 200
        assert controller.status()["users"] == 0
        assert recent_runs(controller.settings.database_path)[0]["status"] == "stopped"
        assert client.post("/api/v1/workloads/stop", headers=headers).status_code == 200
    finally:
        controller.stop()
        server.stop()


def test_zero_application_weights_are_respected():
    mix = normalized_mix({"backup": 1}, APPLICATIONS)
    weights = combined_application_weights("knowledge_worker", mix)
    assert weights["web_saas"] == 0
    assert weights["backup"] > 0


def test_ui_csrf_redirect_and_key_rotation(simulator):
    client = simulator.test_client()
    password = simulator.config["TRAFFICGEN_SETTINGS"].admin_password_path.read_text().strip()
    assert client.post("/login", data={"password": password}).status_code == 400
    client.get("/login")
    with client.session_transaction() as state:
        csrf = state["csrf_token"]
    response = client.post("/login?next=//evil.example/", data={"password": password, "csrf_token": csrf})
    assert response.location == "/"
    assert client.post("/settings/api/rotate").status_code == 400
    client.get("/settings/api")
    old_headers = authorization(simulator)
    with client.session_transaction() as state:
        csrf = state["csrf_token"]
    assert client.post("/settings/api/rotate", data={"csrf_token": csrf}).status_code == 200
    assert client.get("/api/v1/status", headers=old_headers).status_code == 401
    assert client.get("/api/v1/status", headers=authorization(simulator)).status_code == 200
    assert client.get("/settings/api").headers["Cache-Control"] == "no-store"
    assert simulator.config["TRAFFICGEN_SETTINGS"].api_key_path.stat().st_mode & 0o777 == 0o600
    assert client.get("/api/v1/status", headers={"Authorization": "Bearer árvíz"}).status_code == 401


def test_database_migrates_endpoint_and_nullable_samples(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL, run_id TEXT, persona TEXT, application TEXT, request_type TEXT NOT NULL, name TEXT NOT NULL, success INTEGER NOT NULL, response_time_ms REAL, response_length INTEGER NOT NULL DEFAULT 0, error TEXT)")
        conn.execute("CREATE TABLE dem_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL, run_id TEXT, users INTEGER NOT NULL, requests_per_second REAL NOT NULL, failures_per_second REAL NOT NULL, availability_pct REAL NOT NULL, p50_ms REAL, p95_ms REAL, experience_score REAL NOT NULL)")
    init_db(db)
    sample = dict(timestamp=time.time(), users=0, requests_per_second=0, failures_per_second=0, availability_pct=None, experience_score=None)
    record_dem_sample(db, sample)
    assert query_dem_timeseries(db, 0)[0]["experience_score"] is None
    record_transaction(db, dict(success=True, endpoint_id="ep-1", response_time_ms=1))
    assert query_transactions(db, 0)[0]["endpoint_id"] == "ep-1"
    init_db(db)


def test_history_uses_latest_rows_and_fixed_window_rates(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    for number in range(5):
        record_transaction(db, dict(timestamp=time.time() - 10 + number, success=True, response_time_ms=10, application="web_saas"))
    rows = query_transactions(db, 0, limit=2)
    assert len(rows) == 2
    assert rows[0]["timestamp"] < rows[1]["timestamp"]
    assert rows[0]["timestamp"] > time.time() - 8
    assert dem_summary(db, window_seconds=60)["requests_per_second"] == round(5 / 60, 3)


def test_voice_dem_does_not_use_http_latency_threshold():
    row = dict(success=True, response_time_ms=200, response_length=100, application="voice")
    assert summarize_rows([row])["experience_score"] == summarize_rows([row], "voice")["experience_score"]


@pytest.mark.parametrize("echo", [True, False])
def test_udp_dem_measures_delivery_and_rtt(echo):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    stop = threading.Event()
    if echo:
        sock.close()
        worker = gevent.spawn(udp_sink, "127.0.0.1", port, stop)
        gevent.sleep(0.02)
    else:
        worker = None  # Bound socket silently drops requests, like a WAN blackhole.
    environment = Environment()
    events = []
    environment.events.request.add_listener(lambda **event: events.append(event))
    user_class = type("MediaUser", (CorporateUser,), {"abstract": False, "host": "http://127.0.0.1", "runtime_config": dict(run_id="test", personas={"knowledge_worker": 100}, applications={"voice": 100}, target="http://127.0.0.1", udp_port=port)})
    user = user_class(environment)
    user.on_start()
    try:
        user._udp_burst("voice", 160, 5, 0.02)
        assert (events[0]["exception"] is None) is echo
        if echo:
            assert events[0]["response_time"] < 50
            assert events[0]["response_length"] == 60
    finally:
        stop.set()
        sock.close()
        if worker:
            worker.kill()


def test_target_rejects_oversized_upload_and_counts_bytes():
    client = target_app.test_client()
    assert client.post("/files/upload", data=b"abc").json["accepted_bytes"] == 3
    assert client.post("/files/upload", data=b"x" * (16 * 1024 * 1024 + 1)).status_code == 413
