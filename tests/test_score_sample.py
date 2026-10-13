"""A score is graded only from enough transactions, but a small sample still reports its failures."""
from trafficgen.dem import MIN_SCORED_TRANSACTIONS, summarize_rows


def row(app, ok=True, ms=40.0):
    return {"application": app, "success": ok, "response_time_ms": ms if ok else None,
            "response_length": 100 if ok else 0, "error": None if ok else "timeout", "request_type": "HTTP"}


def test_a_small_healthy_sample_shows_no_score_rather_than_a_perfect_one():
    result = summarize_rows([row("dns") for _ in range(MIN_SCORED_TRANSACTIONS - 1)])
    assert result["experience_score"] is None
    assert result["experience"] == "Too few transactions"
    assert result["insufficient_sample"] is True


def test_enough_transactions_are_graded():
    result = summarize_rows([row("dns") for _ in range(MIN_SCORED_TRANSACTIONS)], "dns")
    assert result["experience_score"] == 100
    assert result["insufficient_sample"] is False


def test_a_failed_application_with_a_small_sample_still_drags_the_score_to_zero():
    rows = [row("dns", ok=False) for _ in range(3)] + [row("web_saas") for _ in range(MIN_SCORED_TRANSACTIONS)]
    result = summarize_rows(rows)
    assert result["experience_score"] == 0
    assert result["limiting_application"] == "dns"
    assert result["scoring_complete"] is False


def test_slow_lookups_in_a_small_sample_are_not_graded_into_the_score():
    rows = [row("dns", ms=900.0) for _ in range(3)] + [row("web_saas") for _ in range(MIN_SCORED_TRANSACTIONS)]
    assert summarize_rows(rows)["experience_score"] == 100


if __name__ == "__main__":
    import unittest
    unittest.main()
