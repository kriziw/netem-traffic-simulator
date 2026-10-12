"""Inventory registration must not bypass the simulator routing boundary."""
import json
from unittest.mock import patch

from trafficgen import maintenance, remote_target
from trafficgen.config import ensure_api_key


APPLIANCE_ID = "a" * 32
RECORD = {"name": "Proxmox FortiGate", "vendor": "Fortinet", "model": "FortiGate-VM", "firmware": "7.6.7",
          "interface": "eth1", "gateway": "10.250.10.1", "target": "198.18.0.1"}
INTERFACES = [{"interface": "eth1", "addresses": ["10.250.10.10/24"], "up": True}]


def auth(simulator):
    return {"Authorization": "Bearer " + ensure_api_key(simulator.config["TRAFFICGEN_SETTINGS"])}


def test_registration_is_authenticated_idempotent_and_does_not_change_routes(simulator, monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, "ADMIN_DIR", tmp_path)
    client = simulator.test_client()
    assert client.put("/api/v1/network/appliances/" + APPLIANCE_ID, json=RECORD).status_code == 401
    with patch("trafficgen.network.interfaces", return_value=INTERFACES), \
         patch.object(maintenance, "enqueue") as enqueue:
        for _ in range(2):
            reply = client.put("/api/v1/network/appliances/" + APPLIANCE_ID, headers=auth(simulator), json=RECORD)
            assert reply.status_code == 200, reply.json
            assert reply.json["routing_changed"] is False
            assert reply.json["appliance"]["source"] == "10.250.10.10"
        enqueue.assert_not_called()
    rows = json.loads((simulator.config["TRAFFICGEN_SETTINGS"].runtime_dir / "appliances.json").read_text())
    assert len(rows) == 1
    assert rows[0]["id"] == APPLIANCE_ID


def test_registration_rejects_management_routing_and_running_workloads(simulator, monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, "ADMIN_DIR", tmp_path)
    client = simulator.test_client()
    headers = auth(simulator)
    with patch("trafficgen.network.interfaces", return_value=INTERFACES):
        for changes in ({"interface": "eth0"}, {"target": "8.8.8.8"}, {"gateway": "192.168.0.1"},
                        {"api_token": "must-not-be-accepted"}):
            assert client.put("/api/v1/network/appliances/" + APPLIANCE_ID, headers=headers,
                              json=dict(RECORD, **changes)).status_code == 409
        with patch.object(simulator.config["TRAFFICGEN_CONTROLLER"], "status", return_value={"status": "running"}):
            assert client.put("/api/v1/network/appliances/" + APPLIANCE_ID, headers=headers, json=RECORD).status_code == 409
    assert not (simulator.config["TRAFFICGEN_SETTINGS"].runtime_dir / "appliances.json").exists()


def test_registered_appliance_can_be_selected_through_existing_verified_queue(simulator, monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, "ADMIN_DIR", tmp_path)
    client = simulator.test_client()
    headers = auth(simulator)
    with patch("trafficgen.network.interfaces", return_value=INTERFACES):
        assert client.put("/api/v1/network/appliances/" + APPLIANCE_ID, headers=headers, json=RECORD).status_code == 200
    with patch.object(maintenance, "enqueue", return_value={"id": "job", "action": "route"}) as enqueue:
        response = client.post("/api/v1/network/select", headers=headers, json={"appliance_id": APPLIANCE_ID})
    assert response.status_code == 202
    assert enqueue.call_args.args[1] == "configure_path"
    assert enqueue.call_args.args[2]["gateway"] == RECORD["gateway"]


def test_target_management_is_independent_and_never_exports_its_secret(simulator, monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, "ADMIN_DIR", tmp_path)
    client = simulator.test_client()
    assert client.get("/api/v1/target/health").status_code == 401
    assert client.get("/api/v1/target/health", headers=auth(simulator)).json["configured"] is False
    config = {"host": "192.0.2.1", "api_key": "private-target-token", "fingerprint": "a" * 64}
    (tmp_path / remote_target.SECRET_FILE).write_text(json.dumps(config))
    with patch.object(remote_target, "validate_config", return_value=config), \
         patch.object(remote_target.Client, "request", return_value={"service": remote_target.SERVICE, "version": "0.9.1", "ready": True}):
        result = client.get("/api/v1/target/health", headers=auth(simulator))
    assert result.json["connected"] is True
    assert "private-target-token" not in result.text
    with patch.object(remote_target, "validate_config", side_effect=ValueError("private-target-token")):
        result = client.get("/api/v1/target/health", headers=auth(simulator))
    assert result.json["connected"] is False
    assert "private-target-token" not in result.text
