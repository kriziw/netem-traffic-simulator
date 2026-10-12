from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from trafficgen import setup_probe

LINKS = [{"ifname":"eth0","addr_info":[{"family":"inet","local":"192.168.0.10","prefixlen":24}]}]


def test_probe_hints_cannot_target_management_or_arbitrary_commands():
    for gateway in ("192.168.0.1", "127.0.0.1", "198.18.0.1", "10.0.0.1;id", "224.0.0.1"):
        with pytest.raises(ValueError):
            setup_probe.validate({"gateways":[{"gateway":gateway,"kind":"isp1"}]}, LINKS)
    assert setup_probe.validate({"gateways":[{"gateway":"10.251.10.1","kind":"isp1"}]}, LINKS)


def test_probe_requires_a_real_arp_reply_and_uses_no_assigned_source():
    with patch.object(setup_probe.subprocess, "run", return_value=SimpleNamespace(returncode=1,stdout="Unicast reply from 10.251.10.1 [02:00:00:00:00:01] 0.1ms")) as run:
        assert setup_probe.arp("eth2","10.251.10.1")["reachable"] is True
        assert run.call_args.args[0] == ["arping","-D","-c","1","-w","1","-I","eth2","10.251.10.1"]
    with patch.object(setup_probe.subprocess, "run", return_value=SimpleNamespace(returncode=1,stdout="")):
        assert setup_probe.arp("eth2","10.251.10.1")["reachable"] is None


def test_same_router_reachability_never_claims_netem_traversal():
    assert setup_probe.recommendations([{"kind":"appliance","reachable":True}])["roles"] == ["appliance"]
    result = setup_probe.recommendations([{"kind":"isp1","reachable":True}])
    assert result["state"] == "ambiguous" and result["roles"] == ["baseline1","wan1"]
    assert setup_probe.recommendations([{"kind":"isp1","reachable":False}])["state"] == "unknown"


def test_unaddressed_nic_probe_restores_link_and_does_not_assign_routes(tmp_path):
    admin = Mock(ADMIN_DIR=tmp_path)
    admin.management.return_value = "eth0"
    admin.address_table.return_value = LINKS
    admin.interfaces.return_value = [{"interface":"eth2","addresses":[],"up":False}]
    admin.ip_json.return_value = []
    with patch.object(setup_probe, "arp", return_value={"reachable":True,"mac":"02:00:00:00:00:01"}), \
         patch.object(setup_probe.appliance_identity, "lldp_signals", return_value={}), \
         patch.object(setup_probe.appliance_identity, "web_signals") as web:
        report = setup_probe.collect({"gateways":[{"gateway":"10.251.10.1","kind":"isp1"}]},admin)
    assert report["report"]["interfaces"][0]["recommendation"]["state"] == "ambiguous"
    assert [call.args[0] for call in admin.run.call_args_list] == [
        ["ip","link","set","dev","eth2","up"],["ip","link","set","dev","eth2","down"]]
    admin.configure_interface.assert_not_called()
    web.assert_not_called()


def test_probe_ui_is_authenticated_csrf_protected_and_blocked_during_runs(simulator):
    import importlib
    from trafficgen import maintenance
    module = importlib.import_module("trafficgen.app")
    client = simulator.test_client()
    assert client.post("/settings/network-setup/probe").status_code in (302,400)
    with client.session_transaction() as session:
        session["trafficgen_admin"] = True
        session["csrf_token"] = "token"
    with patch.object(module, "address_table", return_value=LINKS), patch.object(maintenance,"enqueue") as queue:
        client.post("/settings/network-setup/probe",data={"csrf_token":"token","isp1_gateway":"10.251.10.1"})
        assert queue.call_args.args[1] == "probe_setup"
        with patch.object(simulator.config["TRAFFICGEN_CONTROLLER"],"status",return_value={"status":"running"}):
            client.post("/settings/network-setup/probe",data={"csrf_token":"token","isp1_gateway":"10.251.10.1"})
        assert queue.call_count == 1
