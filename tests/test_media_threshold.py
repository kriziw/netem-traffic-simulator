"""Realistic mode fails a burst at the experience policy's loss target, so an impact and the score share one threshold."""
from trafficgen.diagnosis import application_detail


def burst(lost, sent=50):
    error = f"UDP packet loss: {lost}/{sent}" if lost else None
    return {"application": "voice", "success": True, "packets_sent": sent, "packets_lost": lost,
            "response_time_ms": 20.0, "response_length": 0, "error": error, "request_type": "UDP"}


def test_the_policy_loss_target_sets_when_a_realistic_burst_fails():
    rows = [burst(lost=3)]
    assert application_detail("voice", rows)["media"]["fail_by_mode"]["realistic"] == 1
    policy = {"voice": {"loss_max_pct": 10.0}}
    assert application_detail("voice", rows, policy)["media"]["fail_by_mode"]["realistic"] == 0


def test_strict_mode_still_fails_any_lost_packet_whatever_the_policy_says():
    policy = {"voice": {"loss_max_pct": 10.0}}
    assert application_detail("voice", [burst(lost=1)], policy)["media"]["fail_by_mode"]["strict"] == 1


if __name__ == "__main__":
    import unittest
    unittest.main()


def test_configured_target_controls_active_findings_even_with_strict_comparison():
    from trafficgen.diagnosis import diagnose
    policy = {"voice": {"loss_max_pct": 10.0}}
    rows = [burst(lost=2)] * 20
    detail = application_detail("voice", rows, policy)
    assert detail["media"]["fail_by_policy"] == 0
    assert detail["media"]["fail_by_mode"]["strict"] == 20
    finding = next(item for item in diagnose(rows, {"voice": detail}, "strict")["findings"] if item["id"] == "media_loss")
    assert finding["severity"] == "info"
    assert "Configured loss target 10%" in finding["detail"]


def test_start_and_mode_adjustment_preserve_configured_targets(simulator):
    controller = simulator.config["TRAFFICGEN_CONTROLLER"]
    controller.start({"users": 1, "media_mode": "strict", "experience_policy": {"voice": {"loss_max_pct": 10}}})
    assert controller.run["policy"]["voice"]["loss_max_pct"] == 10
    controller.adjust({"media_mode": "realistic"})
    controller.adjust({"media_mode": "strict"})
    assert controller.run["policy"]["voice"]["loss_max_pct"] == 10
