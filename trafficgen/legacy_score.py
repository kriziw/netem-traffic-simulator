# Frozen comparison model from simulator main f0aea4c (0.19.4).
# Shadow output only: never used for v2 acceptance or persisted primary scores.
from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict

from .database import query_transactions
from .profiles import APPLICATIONS
from .experience_policy import default_policy, load_policy


# An application needs this many transactions in the window before its own score is shown: with fewer, one late lookup
# or burst decides it. Its failures still count (see summarize_rows).
MIN_APP_TRANSACTIONS = 5
# The overall score needs this many transactions across the window to be complete; below it the result says so.
MIN_SCORED_TRANSACTIONS = 20


def percentile(values, pct):
    clean = sorted(float(value) for value in values if value is not None)
    if not clean:
        return None
    position = (len(clean) - 1) * pct
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return clean[low]
    fraction = position - low
    return clean[low] + (clean[high] - clean[low]) * fraction


def latency_score(p95_ms, application=None):
    if p95_ms is None:
        return None
    profile = APPLICATIONS.get(application or "", {})
    good = float(profile.get("latency_good_ms", 400))
    poor = float(profile.get("latency_poor_ms", 1800))
    value = float(p95_ms)
    if value <= good:
        return 100.0
    if value >= poor * 2:
        return 0.0
    if value <= poor:
        return 100.0 - ((value - good) / max(1.0, poor - good)) * 65.0
    return max(0.0, 35.0 - ((value - poor) / max(1.0, poor)) * 35.0)


def reliability_score(availability):
    """99% success is healthy; 95% is fair and 90% is critical.

    Raw availability is not an experience score: 5% failed work must not
    contribute 95 points toward a green result.
    """
    if availability >= 99:
        return 100.0
    if availability >= 95:
        return 55.0 + (availability - 95.0) * 45.0 / 4.0
    return max(0.0, (availability - 90.0) * 55.0 / 5.0)


def timeliness_score(share_pct):
    """The share of work that met its target, scored on a slope rather than a cliff.

    Late work that still succeeds is degradation: 99% on time is excellent, 95% fair and 90% poor, falling to
    zero only when half the work is late. Failed work keeps the strict reliability scale above.
    """
    if share_pct >= 99:
        return 100.0
    knots = ((50.0, 0.0), (75.0, 15.0), (90.0, 40.0), (95.0, 60.0), (99.0, 100.0))
    if share_pct <= knots[0][0]:
        return 0.0
    for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
        if share_pct <= x1:
            return y0 + (share_pct - x0) * (y1 - y0) / (x1 - x0)
    return 100.0


def score_label(score):
    if score is None:
        return "No data"
    if score >= 90:
        return "Excellent"
    if score >= 75:
        return "Good"
    if score >= 55:
        return "Fair"
    if score >= 35:
        return "Poor"
    return "Critical"


def quality_components(rows, targets, window_seconds=60):
    """Score fulfillment of usable-work targets, retaining measurement coverage."""
    components, evidence, missing = {}, {}, []
    timed = [row for row in rows if not row["success"] or row.get("response_time_ms") is not None]
    if len(timed) != len(rows):
        missing.append("deadline")
    if timed:
        on_time = sum(bool(row["success"] and row["response_time_ms"] <= targets["deadline_ms"]) for row in timed)
        evidence["on_time_pct"] = round(on_time * 100 / len(timed), 3)
        evidence["deadline_misses"] = sum(bool(row["success"] and row["response_time_ms"] > targets["deadline_ms"]) for row in timed)
        components["deadline"] = timeliness_score(on_time * 100 / len(timed))
    bursts = [row for row in rows if row.get("packets_sent")]
    if bursts:
        known = [row for row in bursts if row.get("packets_lost") is not None]
        if len(known) != len(bursts):
            missing.append("udp_loss")
        if known:
            sent = sum(row["packets_sent"] for row in known)
            lost = sum(row["packets_lost"] for row in known)
            evidence["udp_loss_pct"] = round(lost * 100 / sent, 3)
            met = sum(row["packets_lost"] * 100 / row["packets_sent"] <= targets["loss_max_pct"] for row in known)
            components["udp_loss"] = timeliness_score(met * 100 / len(known))
        if targets["jitter_max_ms"] > 0:
            known = [row for row in bursts if row.get("udp_jitter_ms") is not None]
            if len(known) != len(bursts):
                missing.append("udp_rtt_variation")
            if known:
                evidence["udp_rtt_variation_p95_ms"] = round(percentile([row["udp_jitter_ms"] for row in known], .95), 3)
                met = sum(row["udp_jitter_ms"] <= targets["jitter_max_ms"] for row in known)
                components["udp_rtt_variation"] = timeliness_score(met * 100 / len(known))
    for direction, field in (("down", "response_length"), ("up", "bytes_up")):
        sustained_minimum = targets.get(f"min_sustained_{direction}_mbps", 0)
        if sustained_minimum > 0:
            transfers = [row for row in rows if (row.get(field) or 0) >= 65536]
            if not transfers:
                missing.append(f"sustained_{direction}")
            else:
                rate = sum(row[field] for row in transfers if row["success"]) * 8 / (window_seconds * 1000000)
                evidence[f"sustained_{direction}_mbps"] = round(rate, 3)
                # A missed explicit minimum cannot label the application good.
                components[f"sustained_{direction}"] = 100.0 if rate >= sustained_minimum else min(74.0, 100 * rate / sustained_minimum)
        minimum = targets[f"min_{direction}_mbps"]
        if minimum <= 0:
            continue
        transfers = [row for row in rows if (row.get(field) or 0) >= 65536]
        known = [row for row in transfers if row.get("response_time_ms") and row["response_time_ms"] > 0]
        if not known or len(known) != len(transfers):
            missing.append(f"throughput_{direction}")
        if known:
            rates = [row[field] * 8 / (row["response_time_ms"] * 1000) if row["success"] else 0 for row in known]
            evidence[f"transfer_{direction}_p05_mbps"] = round(percentile(rates, .05), 3)
            components[f"throughput_{direction}"] = timeliness_score(sum(rate >= minimum for rate in rates) * 100 / len(rates))
    return components, evidence, missing


def legacy_summary(rows, application=None, policy=None, window_seconds=60, minimum_transactions=None):
    policy = policy if policy is not None else default_policy()
    total = len(rows)
    override = minimum_transactions
    if minimum_transactions is None:
        minimum_transactions = MIN_APP_TRANSACTIONS if application is not None else MIN_SCORED_TRANSACTIONS
    if not total:
        return {
            "requests": 0,
            "successes": 0,
            "failures": 0,
            "availability_pct": None,
            "p50_ms": None,
            "p95_ms": None,
            "average_ms": None,
            "experience_score": None,
            "experience": "No data",
            "bytes": 0,
            "scoring_complete": False,
            "limiting_application": None,
        }
    successes = [row for row in rows if row["success"]]
    latencies = [
        float(row["response_time_ms"])
        for row in successes
        if row["response_time_ms"] is not None
    ]
    availability = len(successes) * 100.0 / total
    p95 = percentile(latencies, 0.95)
    if application is not None:
        targets = policy.get(application, {"critical": True, "deadline_ms": 400,
                            "jitter_max_ms": 0, "loss_max_pct": 0,
                            "min_down_mbps": 0, "min_up_mbps": 0})
        latency_component = latency_score(p95, application)
        components, evidence, missing = quality_components(rows, targets, window_seconds)
        components["reliability"] = reliability_score(availability)
        if latency_component is not None:
            components["response_time"] = latency_component
        score = min(components.values())
        extra = {"critical": targets["critical"], "score_components": components,
                 "quality": evidence, "experience_targets": targets, "missing_measurements": missing,
                 "scoring_complete": not missing and len(rows) >= minimum_transactions, "limiting_application": application}
    else:
        groups = defaultdict(list)
        for row in rows:
            groups[row.get("application") or "other"].append(row)
        # Include failed-only applications. Healthy/bulk traffic must never
        # average away an outage in a less frequently used business application.
        summaries = {key: legacy_summary(items, application=key, policy=policy, window_seconds=window_seconds,
                                         minimum_transactions=override) for key, items in groups.items()}
        critical = {key: item for key, item in summaries.items() if item["critical"]}
        # An application with too few transactions shows a score only if it failed; see below. A healthy one is left out.
        scored = {key: item for key, item in critical.items() if item["experience_score"] is not None}
        limiting = min(scored, key=lambda key: scored[key]["experience_score"]) if scored else None
        score = scored[limiting]["experience_score"] if limiting else None
        extra = {"limiting_application": limiting, "critical_applications": sorted(critical),
                 "scoring_complete": bool(critical) and all(item["scoring_complete"] for item in critical.values()),
                 "missing_measurements": {key: item["missing_measurements"] for key, item in critical.items() if item["missing_measurements"]}}
    insufficient = total < minimum_transactions
    if application is not None and insufficient:
        # Too few transactions to grade. Availability still shows failures: a failed application scores its availability,
        # while a healthy one shows no score rather than a perfect one.
        floor = reliability_score(availability)
        score = floor if floor < 100 else None
    extra["scoring_complete"] = extra["scoring_complete"] and not insufficient
    extra["insufficient_sample"] = insufficient
    score = max(0.0, min(100.0, score)) if score is not None else None
    return {
        "requests": total,
        "successes": len(successes),
        "failures": total - len(successes),
        "availability_pct": round(availability, 3),
        "p50_ms": round(percentile(latencies, 0.50), 3) if latencies else None,
        "p95_ms": round(p95, 3) if p95 is not None else None,
        "average_ms": round(statistics.mean(latencies), 3) if latencies else None,
        "experience_score": round(score, 2) if score is not None else None,
        "experience": "Too few transactions" if score is None and insufficient else (
            "Incomplete evidence" if score is not None and score >= 75 and not extra["scoring_complete"] else score_label(score)),
        "bytes": sum(int(row["response_length"] or 0) for row in rows),
        **extra,
    }

