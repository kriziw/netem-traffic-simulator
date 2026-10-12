import json
from unittest.mock import patch

import pytest

from trafficgen import maintenance, network_setup, traffic_paths

RAW = dict(role="wan1", name="WAN1 calibration", bridge="vmbr1", interface="eth2", address="10.251.10.10/24",
           gateway="10.251.10.1", target="198.18.0.1", wan_id="wan1", wiring_confirmed="1", action="save", csrf_token="token")
NICS = [{"interface":"eth2", "addresses":["10.251.10.10/24"], "up":True, "carrier":True}]


def test_setup_reuses_saved_identity_but_new_wiring_gets_a_new_policy():
    row = network_setup.definition(RAW)
    assert row["source"] == "10.251.10.10" and row["mode"] == "direct_netem"
    assert network_setup.definition(RAW, row)["id"] == row["id"]
    assert network_setup.definition(dict(RAW, address="10.251.10.11/24"), row)["id"] != row["id"]
    for change in ({"interface":"eth0"}, {"gateway":"192.168.1.1"}, {"target":"8.8.8.8"}, {"bridge":"vmbr1;id"}, {"role":"shell"}):
        with pytest.raises(ValueError):
            network_setup.definition(dict(RAW, **change))


def test_detection_prefills_addresses_without_claiming_bridge_discovery():
    cards = network_setup.cards({}, NICS, [])
    wan = next(row for row in cards if row["role"] == "wan1")
    assert wan["interface"] == "eth2" and wan["address_ready"]
    assert wan["bridge"] == "vmbr1"
    assert next(row for row in cards if row["role"] == "wan2")["nic"] is None


def test_worker_validates_before_mutation_and_reports_partial_success():
    from trafficgen import host_admin
    row = network_setup.definition(RAW)
    with patch.object(network_setup, "validate_interface", side_effect=ValueError("management overlap")), \
         patch.object(host_admin, "configure_interface") as configure:
        with pytest.raises(ValueError, match="overlap"):
            network_setup.configure({"path":network_setup.payload(row), "address":row["address"]}, host_admin)
        configure.assert_not_called()
    with patch.object(network_setup, "validate_interface", return_value={"interface":"eth2","address":row["address"]}), \
         patch.object(host_admin, "configure_interface") as configure, \
         patch.object(traffic_paths, "configure", side_effect=ValueError("Target unavailable")):
        with pytest.raises(ValueError, match="Interface address saved, but path verification failed"):
            network_setup.configure({"path":network_setup.payload(row), "address":row["address"]}, host_admin)
        configure.assert_called_once()


def test_setup_authentication_save_apply_idle_guard_and_errors(simulator, tmp_path, monkeypatch):
    import importlib
    module = importlib.import_module("trafficgen.app")
    monkeypatch.setattr(maintenance, "ADMIN_DIR", tmp_path)
    client = simulator.test_client()
    assert client.get("/settings/network-setup").status_code == 302
    with client.session_transaction() as session:
        session["trafficgen_admin"] = True
        session["csrf_token"] = "token"
    with patch.object(module, "interfaces", return_value=NICS), patch.object(module, "validate_interface") as valid, \
         patch.object(maintenance, "enqueue", return_value={"id":"job"}) as enqueue:
        assert client.post("/settings/network-setup", data=dict(RAW, csrf_token="wrong")).status_code == 400
        assert client.post("/settings/network-setup", data=dict(RAW, wiring_confirmed="")).status_code == 400
        assert client.post("/settings/network-setup", data=RAW).status_code == 302
        enqueue.assert_not_called()
        saved = json.loads((simulator.config["TRAFFICGEN_SETTINGS"].runtime_dir / network_setup.FILE).read_text())
        assert saved["wan1"]["bridge"] == "vmbr1"
        response = client.get("/settings/network-setup")
        assert response.status_code == 200
        assert b"Configure address &amp; verify path" in response.data
        assert b"not detected Proxmox attachments" in response.data
        assert client.post("/settings/network-setup", data=dict(RAW, action="configure")).status_code == 302
        assert enqueue.call_args.args[1] == "setup_path"
        assert enqueue.call_args.args[2]["path"]["id"] == saved["wan1"]["id"]
        assert "bridge" not in enqueue.call_args.args[2]["path"]
        valid.assert_called_once()
        with patch.object(simulator.config["TRAFFICGEN_CONTROLLER"], "status", return_value={"status":"running"}):
            assert client.post("/settings/network-setup", data=dict(RAW, action="configure")).status_code == 400
        assert enqueue.call_count == 1
        bad = client.post("/settings/network-setup", data=dict(RAW, gateway="8.8.8.8"))
        assert bad.status_code == 400 and b'value="8.8.8.8"' in bad.data
