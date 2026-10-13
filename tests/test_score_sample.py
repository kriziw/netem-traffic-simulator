"""A score is graded only from enough transactions, but a small sample still reports its failures."""
from trafficgen.dem import MIN_APP_TRANSACTIONS, MIN_SCORED_TRANSACTIONS, summarize_rows


def row(app, ok=True, ms=40.0):
    return {"application": app, "success": ok, "response_time_ms": ms if ok else None,
            "response_length": 100 if ok else 0, "error": None if ok else "timeout", "request_type": "HTTP"}


def test_an_application_with_too_few_transactions_shows_no_score_rather_than_a_perfect_one():
    result = summarize_rows([row("dns") for _ in range(MIN_APP_TRANSACTIONS - 1)], "dns")
    assert result["experience_score"] is None
    assert result["experience"] == "Too few transactions"
    assert result["insufficient_sample"] is True


def test_an_application_with_enough_transactions_is_graded():
    result = summarize_rows([row("dns") for _ in range(MIN_APP_TRANSACTIONS)], "dns")
    assert result["experience_score"] == 100
    assert result["insufficient_sample"] is False


def test_the_overall_score_is_withheld_when_no_application_has_enough_transactions():
    result = summarize_rows([row("dns") for _ in range(MIN_APP_TRANSACTIONS - 1)])
    assert result["experience_score"] is None
    assert result["experience"] == "Too few transactions"


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
    assert result["experience_score"] is None
    assert "dns" in result["missing_measurements"]
    assert result["scoring_complete"] is False


def test_slow_lookups_in_a_small_sample_are_not_graded_into_the_score():
    rows = [row("dns", ms=900.0) for _ in range(3)] + [row("web_saas") for _ in range(MIN_SCORED_TRANSACTIONS)]
    assert summarize_rows(rows)["experience_score"] is None


if __name__ == "__main__":
    import unittest
    unittest.main()
