"""Paced bulk calibration: runs carry a target rate, and each user paces bounded downloads to its share of it."""
from unittest.mock import Mock, patch

import pytest

from trafficgen import engine


def controller(simulator):
    return simulator.config["TRAFFICGEN_CONTROLLER"]


def paced_user(rate_mbps_per_user):
    user = object.__new__(engine.CorporateUser)
    user.runtime_config = {"run_id": "run-test", "paced_mbps_per_user": rate_mbps_per_user}
    user.endpoint_id = "ep-test"
    user.persona = "office"
    user.client = Mock()
    user.paced_offset_taken = True
    return user


def test_a_target_rate_is_shared_across_the_users(simulator):
    run = controller(simulator)._validate_start({"users": 50, "paced_mbps": 200})
    assert run["paced_mbps"] == 200
    assert run["paced_mbps_per_user"] == 4.0


def test_runs_without_a_target_rate_keep_the_persona_mix(simulator):
    run = controller(simulator)._validate_start({"users": 50})
    assert run["paced_mbps"] is None
    assert run["paced_mbps_per_user"] is None


@pytest.mark.parametrize("value", [0.01, 5000, "fast", float("nan")])
def test_an_unreasonable_target_rate_is_refused(simulator, value):
    with pytest.raises(ValueError):
        controller(simulator)._validate_start({"users": 50, "paced_mbps": value})


def test_paced_users_do_not_wait_between_transfers_and_skip_the_persona_mix():
    user = paced_user(4.0)
    with patch.object(user, "_paced_bulk") as paced, patch.object(user, "_app_web_saas") as web:
        assert user.wait_time() == 0
        user.corporate_action()
    paced.assert_called_once_with()
    web.assert_not_called()


def test_a_fast_transfer_is_paced_to_the_users_share_of_the_rate():
    user = paced_user(4.0)
    with patch.object(engine.gevent, "sleep") as sleep, patch.object(engine.random, "choice", return_value=4096):
        user._paced_bulk()
    # 4096 KiB at 4 Mbit/s takes about 8.39 s; the transfer itself took almost no time.
    assert sleep.call_count == 1
    assert sleep.call_args.args[0] == pytest.approx(4096 * 1024 * 8 / 4_000_000, abs=0.05)
    assert user.client.get.call_args.args[0] == "/files/download?kb=4096"


def test_each_user_starts_at_a_random_offset_once():
    user = paced_user(4.0)
    user.paced_offset_taken = False
    with patch.object(engine.gevent, "sleep") as sleep, patch.object(engine.random, "uniform", return_value=3.0),          patch.object(engine.random, "choice", return_value=1024):
        user._paced_bulk()
        user._paced_bulk()
    offsets = [call.args[0] for call in sleep.call_args_list if call.args[0] == 3.0]
    assert offsets == [3.0]
    assert user.paced_offset_taken is True


def test_transfers_use_random_sizes_so_users_do_not_run_in_lockstep():
    user = paced_user(4.0)
    with patch.object(engine.gevent, "sleep"):
        for _ in range(40):
            user._paced_bulk()
    urls = {call.args[0] for call in user.client.get.call_args_list}
    assert len(urls) > 1
    assert urls <= {f"/files/download?kb={kb}" for kb in engine.PACED_OBJECT_SIZES_KB}


def test_a_slow_link_takes_longer_and_is_not_padded():
    user = paced_user(4.0)
    with patch.object(engine.gevent, "sleep") as sleep, patch.object(engine.time, "monotonic", side_effect=[0.0, 30.0]):
        user._paced_bulk()
    sleep.assert_not_called()
