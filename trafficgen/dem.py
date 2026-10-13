from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict

from .database import query_transactions
from .profiles import APPLICATIONS
from .experience_policy import default_policy, load_policy


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


def summarize_rows(rows, application=None, policy=None, window_seconds=60):
    policy = policy if policy is not None else default_policy()
    total = len(rows)
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
                 "scoring_complete": not missing, "limiting_application": application}
    else:
        groups = defaultdict(list)
        for row in rows:
            groups[row.get("application") or "other"].append(row)
        # Include failed-only applications. Healthy/bulk traffic must never
        # average away an outage in a less frequently used business application.
        summaries = {key: summarize_rows(items, application=key, policy=policy, window_seconds=window_seconds) for key, items in groups.items()}
        critical = {key: item for key, item in summaries.items() if item["critical"]}
        limiting = min(critical, key=lambda key: critical[key]["experience_score"]) if critical else None
        score = critical[limiting]["experience_score"] if limiting else None
        extra = {"limiting_application": limiting, "critical_applications": sorted(critical),
                 "scoring_complete": bool(critical) and all(item["scoring_complete"] for item in critical.values()),
                 "missing_measurements": {key: item["missing_measurements"] for key, item in critical.items() if item["missing_measurements"]}}
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
        "experience": "Incomplete evidence" if score is not None and score >= 75 and not extra["scoring_complete"] else score_label(score),
        "bytes": sum(int(row["response_length"] or 0) for row in rows),
        **extra,
    }


def dem_summary(database_path, run_id=None, window_seconds=60, media_mode="strict", diagnosis=True):
    # Imported here: diagnosis builds on this module's summaries.
    from .diagnosis import application_detail, diagnose

    now = time.time()
    window_seconds = max(10, min(3600, int(window_seconds)))
    since = now - window_seconds
    rows = query_transactions(database_path, since, run_id=run_id, limit=50000)
    policy = load_policy(database_path)
    overall = summarize_rows(rows, policy=policy, window_seconds=window_seconds)

    by_app = defaultdict(list)
    by_persona = defaultdict(list)
    by_endpoint = defaultdict(list)
    endpoint_persona = {}
    for row in rows:
        by_app[row["application"] or "other"].append(row)
        by_persona[row["persona"] or "unknown"].append(row)
        endpoint_id = row["endpoint_id"] or "unknown"
        by_endpoint[endpoint_id].append(row)
        if endpoint_id != "unknown":
            endpoint_persona[endpoint_id] = row["persona"] or "unknown"

    app_summary = {
        key: summarize_rows(items, application=key, policy=policy, window_seconds=window_seconds)
        for key, items in sorted(by_app.items())
    }
    if diagnosis:
        for key, items in by_app.items():
            app_summary[key].update(application_detail(key, items))
    persona_summary = {
        key: summarize_rows(items, policy=policy, window_seconds=window_seconds)
        for key, items in sorted(by_persona.items())
    }
    endpoint_summary = {}
    for key, items in by_endpoint.items():
        if key == "unknown":
            continue
        summary = summarize_rows(items, policy=policy, window_seconds=window_seconds)
        summary["endpoint_id"] = key
        summary["persona"] = endpoint_persona.get(key, "unknown")
        endpoint_summary[key] = summary

    if rows:
        duration = float(window_seconds)
        rps = len(rows) / duration
        failures_per_second = sum(1 for row in rows if not row["success"]) / duration
    else:
        rps = 0.0
        failures_per_second = 0.0

    result = {
        "truncated": len(rows) >= 50000,
        "timestamp": now,
        "window_seconds": window_seconds,
        **overall,
        "requests_per_second": round(rps, 3),
        "failures_per_second": round(failures_per_second, 3),
        "applications": app_summary,
        "personas": persona_summary,
        "endpoints": endpoint_summary,
    }
    if diagnosis:
        result["diagnosis"] = diagnose(rows, app_summary, media_mode, scoring_policy=policy, window_seconds=window_seconds)
        result["interactive_p95_ms"] = result["diagnosis"]["interactive_p95_ms"]
    return result
