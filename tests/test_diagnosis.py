import json
import socket
import sqlite3
import struct
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import gevent
import pytest
import requests
from locust.env import Environment
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError, ReadTimeoutError

from trafficgen.database import init_db, query_transactions, record_transaction, recent_runs
from trafficgen.dem import dem_summary
from trafficgen.diagnosis import (MediaFailure, application_detail, classify_error_text, classify_failure,
                                  diagnose, response_details, target_finding)
from trafficgen.engine import CorporateUser
from trafficgen.target import app as target_app, udp_sink
from trafficgen.wire import ADDRESS_REQUEST, OBSERVED_SOURCE_HEADER


def chained(outer, inner):
    try:
        try:
            raise inner
        except Exception:
            raise outer
    except Exception as exc:
        return exc


@pytest.mark.parametrize("exc, cause", [
    (requests.exceptions.ConnectTimeout("connect"), "connect_timeout"),
    (ConnectTimeoutError(None, "Connection to 198.18.0.1 timed out. (connect timeout=5)"), "connect_timeout"),
    # urllib3 derives NewConnectionError from ConnectTimeoutError; a refusal is not a timeout.
    (NewConnectionError(None, "Failed to establish a new connection: [Errno 111] Connection refused"), "connection_error"),
    (requests.exceptions.ReadTimeout("read"), "read_timeout"),
    (ReadTimeoutError(None, "/", "Read timed out. (read timeout=10)"), "read_timeout"),
    (ConnectionResetError(104, "Connection reset by peer"), "connection_error"),
    (chained(requests.exceptions.ConnectionError("wrapped"), ConnectionRefusedError(111, "refused")), "connection_error"),
    (requests.exceptions.ConnectionError("Connection aborted."), "connection_error"),
    (requests.exceptions.HTTPError("403 Client Error", response=SimpleNamespace(status_code=403)), "http_status"),
    (RuntimeError("Controlled target returned an unexpected redirect."), "redirect"),
    (MediaFailure("media_no_reply", "No UDP replies"), "media_no_reply"),
    (None, None),
])
def test_failures_are_classified_by_cause(exc, cause):
    assert classify_failure(exc) == cause


def test_legacy_error_text_keeps_media_meaning():
    assert classify_error_text("UDP packet loss: 200/200") == "media_no_reply"
    assert classify_error_text("UDP packet loss: 1/200") == "media_loss"
    assert classify_error_text("something odd") == "other"


def test_response_details_split_wait_and_transfer():
    response = SimpleNamespace(status_code=200, elapsed=timedelta(milliseconds=40),
                               request=SimpleNamespace(body=b"x" * 2048),
                               headers={OBSERVED_SOURCE_HEADER: "::ffff:198.18.2.2"})
    details = response_details(response, 100.0)
    assert details == {"wait_ms": 40.0, "transfer_ms": 60.0, "bytes_up": 2048, "status_code": 200, "egress": "198.18.2.2"}
    # Errors caught before a response carry no timing split or egress.
    failed = SimpleNamespace(status_code=0, elapsed=timedelta(0), request=None, headers={})
    assert response_details(failed, 5000.0) == {"wait_ms": None, "transfer_ms": None, "bytes_up": None,
                                                "status_code": None, "egress": None}
    assert response_details(response, 100.0)["egress"] and response_details(None, 1)["egress"] is None


def row(**values):
    base = dict(timestamp=time.time(), run_id="run-1", endpoint_id="ep-1", persona="knowledge_worker",
                request_type="HTTP", name="request", success=True, response_time_ms=50.0, response_length=1000,
                error=None, cause=None, wait_ms=20.0, transfer_ms=30.0, bytes_up=0, status_code=200,
                packets_sent=None, packets_lost=None, egress="198.18.1.2")
    base.update(values)
    return base


def burst(app, sent, lost, egress="198.18.2.2"):
    failed = lost > 0
    cause = None if not failed else ("media_no_reply" if lost >= sent else "media_loss")
    return row(application=app, request_type="UDP", success=not failed, cause=cause, packets_sent=sent,
               packets_lost=lost, wait_ms=None, transfer_ms=None, status_code=None,
               egress=None if lost >= sent else egress)


def run_diagnosis(rows, mode="strict"):
    apps = {}
    for item in rows:
        apps.setdefault(item["application"], []).append(item)
    details = {app: application_detail(app, items) for app, items in apps.items()}
    return details, diagnose(rows, details, mode)


def test_media_loss_reports_both_modes_and_the_egress():
    rows = [burst("voice", 50, 1)] * 3 + [burst("voice", 50, 2)] + [burst("voice", 50, 0)] * 6 + [burst("voice", 50, 50)]
    details, result = run_diagnosis(rows)
    media = details["voice"]["media"]
    assert media["fail_by_mode"] == {"strict": 5, "realistic": 2}
    assert media["bursts_with_loss"] == 4 and media["no_reply"] == 1
    loss = next(item for item in result["findings"] if item["id"] == "media_loss")
    assert loss["by_egress"] == {"198.18.2.2": 4}
    assert "strict 5" in loss["detail"] and "realistic 2" in loss["detail"]
    silent = next(item for item in result["findings"] if item["id"] == "media_no_reply")
    assert silent["severity"] == "bad" and silent["by_egress"] == {"unknown": 1}


def test_bulk_downloads_are_bandwidth_bound_per_egress():
    size = 4 * 1024 * 1024
    rows = [row(application="updates", response_time_ms=4000.0, wait_ms=100.0, transfer_ms=3900.0,
                response_length=size, egress="198.18.1.2") for _ in range(6)]
    rows += [row(application="web_saas") for _ in range(6)]
    details, result = run_diagnosis(rows)
    assert details["updates"]["median_down_mbps"] == pytest.approx(size * 8 / 3900 / 1000, rel=1e-2)
    finding = next(item for item in result["findings"] if item["id"] == "bandwidth_bound")
    assert finding["direction"] == "download" and finding["applications"] == ["updates"]
    assert finding["by_egress"]["198.18.1.2"]["transfers"] == 6
    # Interactive P95 excludes the large transfers that dominate the blended P95.
    assert result["interactive_p95_ms"] == 50.0


def test_slow_wait_and_http_errors_explain_interactive_symptoms():
    rows = [row(application="web_saas", response_time_ms=900.0, wait_ms=880.0, transfer_ms=20.0) for _ in range(6)]
    rows += [row(application="web_saas", success=False, cause="http_status", status_code=403, error="403")] * 2
    rows += [row(application="dns", success=False, cause="connect_timeout", egress=None, wait_ms=None,
                 transfer_ms=None, status_code=None)]
    _details, result = run_diagnosis(rows)
    ids = [item["id"] for item in result["findings"]]
    assert {"slow_wait", "http_errors", "timeouts"} <= set(ids)
    assert next(item for item in result["findings"] if item["id"] == "http_errors")["status_codes"] == {"403": 2}
    assert result["egress"]["unknown"]["causes"] == {"connect_timeout": 1}
    assert result["causes"] == {"http_status": 2, "connect_timeout": 1}


def test_target_finding_only_for_targets_missing_capabilities():
    assert target_finding({"version": "0.7.0", "capabilities": ["media-echo", "observed-source"]}, "0.7.0") is None
    assert target_finding({"error": "unreachable"}, "0.7.0") is None
    finding = target_finding({"version": None, "capabilities": []}, "0.7.0")
    assert finding["id"] == "target_outdated" and "no version" in finding["detail"]


def test_target_reports_source_version_and_answers_old_and_new_media_probes():
    client = target_app.test_client()
    response = client.get("/health", environ_base={"REMOTE_ADDR": "198.18.2.2"})
    assert response.headers[OBSERVED_SOURCE_HEADER] == "198.18.2.2"
    assert {"media-echo", "observed-source"} <= set(response.json["capabilities"]) and response.json["version"]

    stop = threading.Event()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    worker = gevent.spawn(udp_sink, "127.0.0.1", port, stop)
    gevent.sleep(0.02)
    client_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client_sock.settimeout(2)
    try:
        header = b"n" * 8 + struct.pack("!I", 7)
        client_sock.sendto(header + b"m" * 20, ("127.0.0.1", port))
        assert client_sock.recv(64) == header  # older simulators keep the 12-byte echo
        client_sock.sendto(header + ADDRESS_REQUEST + b"m" * 20, ("127.0.0.1", port))
        assert client_sock.recv(64) == header + socket.inet_aton("127.0.0.1")
    finally:
        stop.set()
        client_sock.close()
        worker.kill()


def lossy_echo(port_holder, drop, stop):
    # Echoes media like the target but loses chosen sequence numbers, like a lossy WAN.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.1)
    port_holder.append(sock.getsockname()[1])
    while not stop.is_set():
        try:
            data, peer = sock.recvfrom(2048)
        except socket.timeout:
            continue
        if struct.unpack("!I", data[8:12])[0] not in drop:
            sock.sendto(data[:12] + socket.inet_aton("198.18.2.2"), peer)
    sock.close()


@pytest.mark.parametrize("mode, drop, fails", [
    ("strict", {3}, True), ("realistic", {3}, False), ("realistic", {3, 9}, True), ("realistic", set(range(50)), True),
])
def test_media_mode_decides_how_much_loss_fails_a_burst(mode, drop, fails):
    holder, stop = [], threading.Event()
    worker = gevent.spawn(lossy_echo, holder, drop, stop)
    while not holder:
        gevent.sleep(0.01)
    environment = Environment()
    events = []
    environment.events.request.add_listener(lambda **event: events.append(event))
    config = dict(run_id="t", personas={"knowledge_worker": 100}, applications={"voice": 100},
                  target="http://127.0.0.1", udp_port=holder[0], media_mode=mode)
    user = type("MediaUser", (CorporateUser,), {"abstract": False, "host": "http://127.0.0.1", "runtime_config": config})(environment)
    user.on_start()
    try:
        user._udp_burst("voice", 160, 50, 0.001)
    finally:
        stop.set()
        worker.join(timeout=2)
    event = events[0]
    assert (event["exception"] is not None) is fails
    assert event["context"]["packets_sent"] == 50 and event["context"]["packets_lost"] == len(drop)
    assert event["context"]["egress"] == (None if len(drop) == 50 else "198.18.2.2")
    if len(drop) == 50:
        assert classify_failure(event["exception"]) == "media_no_reply"


def test_new_columns_migrate_and_round_trip(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL, run_id TEXT, endpoint_id TEXT, persona TEXT, application TEXT, request_type TEXT NOT NULL, name TEXT NOT NULL, success INTEGER NOT NULL, response_time_ms REAL, response_length INTEGER NOT NULL DEFAULT 0, error TEXT)")
        conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL, status TEXT NOT NULL, profile TEXT NOT NULL, target TEXT NOT NULL, target_users INTEGER NOT NULL, spawn_rate REAL NOT NULL, activity TEXT NOT NULL, personas_json TEXT NOT NULL, applications_json TEXT NOT NULL)")
        conn.execute("INSERT INTO runs VALUES ('old', 1, 2, 'stopped', 'office', 'http://x', 1, 1, 'normal', '{}', '{}')")
        conn.execute("INSERT INTO transactions (timestamp, request_type, name, success, error) VALUES (?, 'UDP', 'voice', 0, 'UDP packet loss: 50/50')", (time.time(),))
    init_db(db)
    init_db(db)
    assert recent_runs(db)[0]["media_mode"] == "strict"
    record_transaction(db, row(application="voice", cause="media_loss", packets_sent=50, packets_lost=1, egress="198.18.2.2"))
    rows = query_transactions(db, 0)
    assert rows[0]["cause"] is None and rows[1]["packets_lost"] == 1 and rows[1]["egress"] == "198.18.2.2"
    # Legacy rows without a stored cause are still explained from their error text.
    assert dem_summary(db, window_seconds=60)["diagnosis"]["causes"]["media_no_reply"] == 1


def test_media_mode_is_validated_and_adjustable(simulator):
    headers = {"Authorization": "Bearer " + simulator.config["TRAFFICGEN_SETTINGS"].api_key_path.read_text().strip()}
    client = simulator.test_client()
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]
    assert client.post("/api/v1/workloads/start", json={"media_mode": "lenient"}, headers=headers).status_code == 400
    assert controller.run is None
    try:
        response = client.post("/api/v1/workloads/start", json={"users": 1, "media_mode": "realistic", "target": "http://127.0.0.1:9", "applications": {"dns": 1}}, headers=headers)
        assert response.status_code == 201 and response.json["run"]["media_mode"] == "realistic"
        assert recent_runs(controller.settings.database_path)[0]["media_mode"] == "realistic"
        assert client.post("/api/v1/workloads/adjust", json={"media_mode": 1}, headers=headers).status_code == 400
        response = client.post("/api/v1/workloads/adjust", json={"media_mode": "strict"}, headers=headers)
        assert response.status_code == 200 and response.json["run"]["media_mode"] == "strict"
        status = client.get("/api/v1/status", headers=headers).json
        assert status["dem"]["diagnosis"]["media_mode"] == "strict"
        assert json.loads(json.dumps(client.get("/api/v1/catalog", headers=headers).json))["media_modes"]["realistic"]["tolerance_pct"] == {"voice": 2.0, "video": 1.0}
    finally:
        controller.stop()
