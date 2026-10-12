import json
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from trafficgen import data_dhcp, maintenance, traffic_paths

LINKS = [{"ifname":"eth0","link_type":"ether","addr_info":[{"family":"inet","local":"192.168.0.10","prefixlen":24}]},
         {"ifname":"eth2","link_type":"ether","addr_info":[]}]
ENV = {"interface":"eth2","ip":"10.251.10.10","subnet":"255.255.255.0","router":"10.251.10.1", "lease":"120"}


def admin_for(tmp_path):
    from trafficgen.host_admin import atomic_json
    admin = Mock(ADMIN_DIR=tmp_path)
    admin.management.return_value = "eth0"
    admin.interfaces.return_value = [{"interface":"eth2","addresses":[],"up":True}]
    admin.address_table.return_value = LINKS
    admin.atomic_json.side_effect = atomic_json
    (tmp_path / data_dhcp.FILE).write_text(json.dumps({"eth2":True}))
    return admin


def test_dhcp_ack_assigns_address_and_discovers_gateway_without_default_routes_or_dns(tmp_path):
    admin = admin_for(tmp_path)
    data_dhcp.event(admin,"bound",ENV)
    lease = data_dhcp.lease(admin,"eth2")
    assert lease["address"] == "10.251.10.10/24" and lease["gateway"] == "10.251.10.1"
    assert lease["expires_at"] > time.time()
    assert [call.args[0] for call in admin.run.call_args_list] == [["ip","-4","address","replace","10.251.10.10/24","dev","eth2"]]
    data_dhcp.event(admin,"renew",ENV)
    assert data_dhcp.lease(admin,"eth2")["state"] == "bound"


def test_invalid_or_management_overlapping_dhcp_reply_is_rejected_before_mutation(tmp_path):
    admin = admin_for(tmp_path)
    for change in ({"ip":"192.168.0.20"}, {"subnet":"not-a-mask"}, {"router":"10.252.10.1"}, {"ip":"198.18.0.2"}):
        with pytest.raises(ValueError):
            data_dhcp.event(admin,"bound",dict(ENV,**change))
    admin.run.assert_not_called()
    with pytest.raises(ValueError):
        data_dhcp.event(admin,"bound",dict(ENV,interface="eth0"))


def test_lease_loss_removes_only_the_address_owned_by_dhcp(tmp_path):
    admin = admin_for(tmp_path)
    data_dhcp.event(admin,"bound",ENV)
    admin.interfaces.return_value = [{"interface":"eth2","addresses":["10.251.10.10/24"],"up":True}]
    data_dhcp.event(admin,"deconfig",ENV)
    assert admin.run.call_args.args[0] == ["ip","-4","address","del","10.251.10.10/24","dev","eth2"]
    assert data_dhcp.lease(admin,"eth2")["state"] == "waiting"


def test_expired_lease_cannot_start_or_probe_an_old_selected_path(tmp_path,monkeypatch):
    admin = admin_for(tmp_path)
    monkeypatch.setattr(maintenance,"ADMIN_DIR",tmp_path)
    admin.atomic_json(tmp_path / "dhcp-eth2.json",dict(state="bound",source="10.251.10.10",expires_at=time.time()-1))
    result = traffic_paths.policy_status(dict(interface="eth2",source="10.251.10.10"))
    assert not result["active"] and "lease expired or changed" in result["message"]


def test_existing_address_cannot_be_taken_over_by_app_dhcp(tmp_path):
    admin = admin_for(tmp_path)
    (tmp_path / data_dhcp.FILE).write_text('{}')
    admin.interfaces.return_value = [{"interface":"eth2","addresses":["10.251.10.10/24"],"up":True}]
    with pytest.raises(ValueError,match="already has an address"):
        data_dhcp.configure({"interface":"eth2"},admin,wait=0)
    admin.run.assert_not_called()


def test_dhcp_ui_queues_one_idle_data_nic_and_does_not_accept_management(simulator,tmp_path,monkeypatch):
    import importlib
    module = importlib.import_module('trafficgen.app')
    monkeypatch.setattr(maintenance,"ADMIN_DIR",tmp_path)
    client = simulator.test_client()
    with client.session_transaction() as session:
        session['trafficgen_admin']=True
        session['csrf_token']='token'
    with patch.object(module,'interfaces',return_value=[{'interface':'eth2','addresses':[],'up':True,'carrier':True}]), \
         patch.object(maintenance,'enqueue') as queue:
        assert client.post('/settings/network-setup/dhcp',data={'csrf_token':'token','interface':'eth2'}).status_code == 302
        assert queue.call_args.args[1:] == ('data_dhcp',{'interface':'eth2'})
        client.post('/settings/network-setup/dhcp',data={'csrf_token':'token','interface':'eth0'})
        assert queue.call_count == 1
        with patch.object(simulator.config['TRAFFICGEN_CONTROLLER'],'status',return_value={'status':'running'}):
            client.post('/settings/network-setup/dhcp',data={'csrf_token':'token','interface':'eth2'})
        assert queue.call_count == 1
        response = client.get('/settings/network-setup?interface=eth2&role=baseline1')
        assert response.status_code == 200
        assert b'Get address automatically (DHCP)' in response.data
        assert b'10.251.10.10/24' not in response.data
        assert b'WAN2 calibration' not in response.data


def test_privileged_helpers_import_without_workload_venv_dependencies():
    import subprocess
    import sys
    from pathlib import Path
    code = """
import importlib.abc,sys
class BlockRequests(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,*args):
        if fullname=='requests' or fullname.startswith('requests.'):
            raise ModuleNotFoundError('requests intentionally unavailable in root interpreter')
sys.meta_path.insert(0,BlockRequests())
sys.path.insert(0,sys.argv[1])
from trafficgen import host_admin,data_dhcp,network_setup,traffic_paths
assert 'requests' not in sys.modules
"""
    subprocess.run([sys.executable,'-I','-c',code,str(Path(__file__).resolve().parents[1])],check=True)
