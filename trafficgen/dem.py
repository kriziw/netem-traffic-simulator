from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict

from .database import query_transactions
from .profiles import APPLICATIONS


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
        return 100.0 - ((value - good) / max(1.0, poor - good)) * 45.0
    return max(0.0, 55.0 - ((value - poor) / max(1.0, poor)) * 55.0)


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


def summarize_rows(rows, application=None):
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
        latency_component = latency_score(p95, application)
    else:
        groups = defaultdict(list)
        for row in successes:
            if row["response_time_ms"] is not None:
                groups[row.get("application")].append(float(row["response_time_ms"]))
        latency_component = (sum(len(values) * latency_score(percentile(values, 0.95), key)
                                 for key, values in groups.items()) / sum(map(len, groups.values()))
                             if groups else None)
    score = availability if latency_component is None else availability * 0.65 + latency_component * 0.35
    score = max(0.0, min(100.0, score))
    return {
        "requests": total,
        "successes": len(successes),
        "failures": total - len(successes),
        "availability_pct": round(availability, 3),
        "p50_ms": round(percentile(latencies, 0.50), 3) if latencies else None,
        "p95_ms": round(p95, 3) if p95 is not None else None,
        "average_ms": round(statistics.mean(latencies), 3) if latencies else None,
        "experience_score": round(score, 2),
        "experience": score_label(score),
        "bytes": sum(int(row["response_length"] or 0) for row in rows),
    }


def dem_summary(database_path, run_id=None, window_seconds=60, media_mode="strict", diagnosis=True):
    # Imported here: diagnosis builds on this module's summaries.
    from .diagnosis import application_detail, diagnose

    now = time.time()
    window_seconds = max(10, min(3600, int(window_seconds)))
    since = now - window_seconds
    rows = query_transactions(database_path, since, run_id=run_id, limit=50000)
    overall = summarize_rows(rows)

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
        key: summarize_rows(items, application=key)
        for key, items in sorted(by_app.items())
    }
    if diagnosis:
        for key, items in by_app.items():
            app_summary[key].update(application_detail(key, items))
    persona_summary = {
        key: summarize_rows(items)
        for key, items in sorted(by_persona.items())
    }
    endpoint_summary = {}
    for key, items in by_endpoint.items():
        if key == "unknown":
            continue
        summary = summarize_rows(items)
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
        result["diagnosis"] = diagnose(rows, app_summary, media_mode)
        result["interactive_p95_ms"] = result["diagnosis"]["interactive_p95_ms"]
    return result
