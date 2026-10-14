"""Measured utility is visible for small samples; acceptance retains its evidence gates."""
from trafficgen.dem import MIN_APP_TRANSACTIONS, MIN_SCORED_TRANSACTIONS, summarize_rows


def row(app, ok=True, ms=40.0):
    return {"application": app, "success": ok, "response_time_ms": ms if ok else None,
            "response_length": 100 if ok else 0, "error": None if ok else "timeout", "request_type": "HTTP"}


def test_small_application_sample_shows_utility_without_claiming_complete_evidence():
    result = summarize_rows([row("dns") for _ in range(MIN_APP_TRANSACTIONS - 1)], "dns")
    assert result["experience_score"] == 100
    assert result["scoring_complete"] is False
    assert result["sample_confidence"] == "limited_sample"
    assert result["experience"] == "Incomplete evidence"
    assert result["insufficient_sample"] is True


def test_an_application_with_enough_transactions_is_graded():
    result = summarize_rows([row("dns") for _ in range(MIN_APP_TRANSACTIONS)], "dns")
    assert result["experience_score"] == 100
    assert result["insufficient_sample"] is False


def test_small_overall_sample_shows_measured_utility_without_granting_acceptance():
    result = summarize_rows([row("dns") for _ in range(MIN_APP_TRANSACTIONS - 1)])
    assert result["experience_score"] == 100
    assert result["scoring_complete"] is False
    assert result["sample_confidence"] == "limited_sample"
    assert result["experience"] == "Incomplete evidence"


def test_the_overall_score_is_graded_from_the_applications_that_have_enough_transactions():
    # Thirty-six transactions across six applications: no application reaches twenty, but each reaches five.
    apps = ["web_saas", "collaboration", "voice", "dns", "erp", "mes"]
    rows = [row(app) for app in apps for _ in range(6)]
    result = summarize_rows(rows)
    assert len(rows) >= MIN_SCORED_TRANSACTIONS
    assert result["experience_score"] == 100
    assert result["scoring_complete"] is True


def test_a_failed_application_with_a_small_sample_keeps_overall_evidence_incomplete():
    rows = [row("dns", ok=False) for _ in range(3)] + [row("web_saas") for _ in range(MIN_SCORED_TRANSACTIONS)]
    result = summarize_rows(rows)
    assert result["experience_score"] == 50
    assert result["score_status"] == "provisional"
    assert result["coverage_pct"] == 100
    assert result["evidence_coverage_pct"] == 50
    assert result["business_acceptance"] == "fail"
    assert "dns" in result["missing_measurements"]
    assert result["scoring_complete"] is False


def test_slow_lookups_in_a_small_sample_contribute_to_measured_utility():
    rows = [row("dns", ms=900.0) for _ in range(3)] + [row("web_saas") for _ in range(MIN_SCORED_TRANSACTIONS)]
    result = summarize_rows(rows)
    assert result["experience_score"] == 56.66
    assert result["coverage_pct"] == 100
    assert result["score_range"] == {"minimum": 56.66, "maximum": 56.66}
    assert not result["scoring_complete"]


if __name__ == "__main__":
    import unittest
    unittest.main()
