"""Purpose-based steering boundaries and full-workload affinity."""
import json
import time
from unittest.mock import Mock, patch

import pytest

from trafficgen import host_admin, maintenance, network, traffic_paths
from trafficgen.config import ensure_api_key
from trafficgen.database import create_run, init_db, recent_runs

DEFINITION = {"id": "a" * 32, "name": "Router baseline", "mode": "direct_baseline", "wan_id": "",
              "interface": "eth2", "source": "10.251.10.10", "gateway": "10.251.10.1", "target": "198.18.0.1"}
INTERFACES = [{"interface": "eth2", "addresses": ["10.251.10.10/24", "10.251.10.11/24"], "up": True}]


def test_path_validation_protects_management_and_selects_the_requested_source():
    row = traffic_paths.validate(dict(DEFINITION, source="10.251.10.11"), inventory=INTERFACES)
    assert row["source"] == "10.251.10.11"
    for change in ({"interface": "eth0"}, {"target": "8.8.8.8"}, {"source": "192.168.0.1"}, {"policy_table": 254},
                   {"mode": "direct_netem", "wan_id": ""}, {"id": "../../outside"}, {"wan_id": "wan1"}):
        with pytest.raises(ValueError):
            traffic_paths.validate(dict(DEFINITION, **change), inventory=INTERFACES)


def test_source_adapter_pins_http_and_disables_environment_proxy_fallback():
    import requests
    session = traffic_paths.bind_http(requests.Session(), DEFINITION["source"])
    assert session.trust_env is False
    for scheme in ("http://", "https://"):
        adapter = session.adapters[scheme]
        assert adapter.poolmanager.connection_pool_kw["source_address"] == (DEFINITION["source"], 0)
    session.close()


def test_unmanaged_policy_table_is_never_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(host_admin, "ADMIN_DIR", tmp_path)
    with patch.object(traffic_paths, "validate", return_value=dict(DEFINITION, address="10.251.10.10/24")), \
         patch.object(host_admin, "ip_json", return_value=[{"dst": "default", "protocol": "static"}]), \
         patch.object(host_admin, "run") as run:
        with pytest.raises(ValueError, match="unmanaged"):
            traffic_paths.configure(DEFINITION, host_admin)
    run.assert_not_called()


def test_duplicate_source_cannot_steal_another_purposes_route(tmp_path, monkeypatch):
    monkeypatch.setattr(host_admin, "ADMIN_DIR", tmp_path)
    (tmp_path / traffic_paths.FILE).write_text(json.dumps([dict(DEFINITION, id="b" * 32)]))
    with patch.object(traffic_paths, "validate", return_value=DEFINITION), patch.object(host_admin, "run") as run:
        with pytest.raises(ValueError, match="distinct source"):
            traffic_paths.configure(DEFINITION, host_admin)
    run.assert_not_called()


def test_missing_policy_never_sends_a_probe_on_the_default_route():
    with patch.object(traffic_paths, "policy_status", return_value={"active": False}), \
         patch("trafficgen.traffic_paths.http.client.HTTPConnection") as connection:
        result = traffic_paths.probe(dict(DEFINITION, policy_table=21000))
    assert result["reachable"] is None
    connection.assert_not_called()


def test_policy_selection_is_saved_only_after_target_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(host_admin, "ADMIN_DIR", tmp_path)
    old = {"id": "old", "source": "10.250.10.10", "gateway": "10.250.10.1"}
    (tmp_path / "route.json").write_text(json.dumps(old))
    row = dict(DEFINITION, address="10.251.10.10/24")
    def ip(*args):
        if args[:2] == ("route", "get"):
            return [{"dev": "eth2", "gateway": row["gateway"], "table": 21000}]
        return []
    with patch.object(traffic_paths, "validate", return_value=row), patch.object(host_admin, "ip_json", side_effect=ip), \
         patch.object(host_admin, "run", side_effect=lambda args, **kwargs: '{"service":"wrong"}' if args[0] == "curl" else ""):
        with pytest.raises(ValueError, match="controlled target"):
            traffic_paths.configure(dict(DEFINITION, select=True), host_admin)
    assert json.loads((tmp_path / "route.json").read_text()) == old
    assert not (tmp_path / traffic_paths.FILE).exists()


def test_path_api_is_authenticated_and_cannot_switch_while_busy(simulator, monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, "ADMIN_DIR", tmp_path)
    client = simulator.test_client()
    headers = {"Authorization": "Bearer " + ensure_api_key(simulator.config["TRAFFICGEN_SETTINGS"])}
    assert client.get("/api/v1/network/paths").status_code == 401
    body = {key: value for key, value in DEFINITION.items() if key != "id"}
    with patch.object(traffic_paths, "interfaces", return_value=INTERFACES), \
         patch.object(maintenance, "enqueue", return_value={"id": "job"}) as enqueue:
        result = client.put("/api/v1/network/paths/" + DEFINITION["id"], headers=headers, json=body)
        assert result.status_code == 202, result.json
        assert enqueue.call_args.args[1] == "configure_path"
        with patch.object(simulator.config["TRAFFICGEN_CONTROLLER"], "status", return_value={"status": "running"}):
            assert client.put("/api/v1/network/paths/" + DEFINITION["id"], headers=headers, json=body).status_code == 409
    assert client.get("/traffic-paths").status_code == 302


def test_full_workload_user_limit_is_the_same_for_all_path_modes(simulator):
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]
    for mode in traffic_paths.MODES:
        # No probe-only or smaller direct-path population limit is introduced.
        run = controller._validate_start({"users": 5000, "target": "http://198.18.0.1:8090"})
        assert run["target_users"] == 5000
        assert traffic_paths.metadata(dict(DEFINITION, mode=mode))["mode"] == mode


def test_history_retains_the_path_after_service_restart(tmp_path):
    path = tmp_path / "runs.db"
    init_db(path)
    run = dict(run_id="run-1", started_at=time.time(), status="stopped", profile="office", target="http://198.18.0.1:8090",
               target_users=5000, spawn_rate=50, activity="normal", personas={}, applications={}, traffic_path=DEFINITION)
    create_run(path, run)
    init_db(path)
    assert recent_runs(path)[0]["traffic_path"] == DEFINITION
