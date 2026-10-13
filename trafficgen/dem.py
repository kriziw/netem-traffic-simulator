from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict

from .database import query_transactions, scoring_contract
from .profiles import APPLICATIONS
from .experience_policy import default_policy, load_policy, criticality_weight, SCORE_MODEL


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


def target_credit(value, target):
    """Elastic utility: a small target overrun causes a small loss of useful service."""
    if value is None:
        return None
    return 100.0 if value <= target else 100.0 * target / value


def latency_score(p95_ms, application=None, target_ms=None):
    target = default_policy().get(application, {}).get("deadline_ms", 400) if target_ms is None else target_ms
    return target_credit(p95_ms, target)


def reliability_score(availability, target=100):
    return min(100.0, max(0.0, availability * 100 / target)) if target else 100.0


def timeliness_score(share_pct):
    # Compatibility helper: target compliance is a measured percentage, not an outage severity scale.
    return max(0.0, min(100.0, share_pct))


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
        delivered = [row for row in timed if row["success"]]
        credits = [(100.0 if row["response_time_ms"] <= targets["deadline_ms"] else 0.0)
                   if targets.get("hard_deadline") else target_credit(row["response_time_ms"], targets["deadline_ms"])
                   for row in delivered]
        components["deadline"] = statistics.mean(credits) if credits else 0.0
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
            evidence["loss_target_met_pct"] = met * 100 / len(known)
            components["udp_loss"] = reliability_score(100 - lost * 100 / sent, 100 - targets["loss_max_pct"])
        if targets["jitter_max_ms"] > 0:
            delivered_bursts = [row for row in bursts if row.get("packets_lost", 0) < row["packets_sent"]]
            known = [row for row in delivered_bursts if row.get("udp_jitter_ms") is not None]
            if len(known) != len(delivered_bursts):
                missing.append("udp_rtt_variation")
            if known:
                evidence["udp_rtt_variation_p95_ms"] = round(percentile([row["udp_jitter_ms"] for row in known], .95), 3)
                met = sum(row["udp_jitter_ms"] <= targets["jitter_max_ms"] for row in known)
                evidence["jitter_target_met_pct"] = met * 100 / len(known)
                components["udp_rtt_variation"] = target_credit(evidence["udp_rtt_variation_p95_ms"], targets["jitter_max_ms"])
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
                components[f"sustained_{direction}"] = 100.0 if rate >= sustained_minimum else 100 * rate / sustained_minimum
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
            components[f"throughput_{direction}"] = min(100.0, 100 * percentile(rates, .05) / minimum)
    return components, evidence, missing


def summarize_rows(rows, application=None, policy=None, window_seconds=60, minimum_transactions=None, application_shares=None, scope="application"):
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
            "limiting_application": None, "missing_measurements": ["transactions"], "critical": policy.get(application, {}).get("critical", False),
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
        targets = dict(policy.get(application, {"critical": True, "deadline_ms": 400,
                            "jitter_max_ms": 0, "loss_max_pct": 0,
                            "min_down_mbps": 0, "min_up_mbps": 0}))
        if scope != "application":
            # Site-wide sustained targets are not per-endpoint/persona/egress requirements.
            targets.update(min_sustained_down_mbps=0, min_sustained_up_mbps=0)
        components, evidence, missing = quality_components(rows, targets, window_seconds)
        quality_score = min(components.values(), default=100)
        loss_accounted = sum(not row["success"] and (row.get("packets_sent") or 0) > 0
                             and row.get("packets_lost") == row["packets_sent"] for row in rows)
        transport_pct = (len(successes) + loss_accounted) * 100 / total
        components["reliability"] = reliability_score(transport_pct, targets.get("success_min_pct", 100))
        score = quality_score * components["reliability"] / 100
        compliance = {"success": availability >= targets.get("success_min_pct", 100),
                      "deadline": evidence.get("deadline_misses", 0) == 0}
        if "udp_loss_pct" in evidence:
            compliance["loss"] = evidence["udp_loss_pct"] <= targets["loss_max_pct"]
        if "udp_rtt_variation_p95_ms" in evidence:
            compliance["jitter"] = evidence["udp_rtt_variation_p95_ms"] <= targets["jitter_max_ms"]
        for direction in ("down", "up"):
            for kind, metric, target in (("throughput", "transfer_" + direction + "_p05_mbps", "min_" + direction + "_mbps"),
                                         ("sustained", "sustained_" + direction + "_mbps", "min_sustained_" + direction + "_mbps")):
                if targets.get(target, 0):
                    compliance[kind + "_" + direction] = evidence.get(metric) is not None and evidence[metric] >= targets[target]
        extra = {"criticality": targets.get("criticality", "critical" if targets["critical"] else "standard"),
                 "criticality_weight": criticality_weight(targets), "compliance": compliance,
                 "target_violations": [key for key, met in compliance.items() if not met],
                 "critical": targets["critical"], "score_components": components,
                 "quality": evidence, "experience_targets": targets, "missing_measurements": missing,
                 "scoring_complete": not missing and len(rows) >= minimum_transactions, "limiting_application": application}
    else:
        groups = defaultdict(list)
        for row in rows:
            groups[row.get("application") or "other"].append(row)
        shares = application_shares if application_shares is not None else {key: 1 for key in groups}
        summaries = {key: summarize_rows(groups.get(key, []), application=key, policy=policy,
                        window_seconds=window_seconds, minimum_transactions=override, scope=scope)
                     for key, share in shares.items() if share > 0}
        weights = {key: shares[key] * criticality_weight(policy.get(key, {})) for key in summaries}
        total_weight = sum(weights.values())
        weights = {key: value / total_weight for key, value in weights.items()} if total_weight else {}
        complete = bool(summaries) and all(item["scoring_complete"] for item in summaries.values())
        scored = {key: item for key, item in summaries.items() if item["experience_score"] is not None}
        score = sum(weights[key] * item["experience_score"] for key, item in scored.items()) if len(scored) == len(summaries) and summaries else None
        limiting = max(scored, key=lambda key: weights[key] * (100 - scored[key]["experience_score"])) if scored else None
        contributions = {key: {"configured_share": shares[key], "weight": weights[key],
                         "score": item["experience_score"], "criticality_weight": criticality_weight(policy.get(key, {})),
                         "lost_points": weights[key] * (100 - item["experience_score"]) if item["experience_score"] is not None else None,
                         "target_violations": item.get("target_violations", []), "requests": item["requests"],
                         "scoring_complete": item["scoring_complete"]} for key, item in summaries.items()}
        extra = {"limiting_application": limiting, "application_contributions": contributions,
                 "critical_applications": sorted(key for key in summaries if policy.get(key, {}).get("critical")),
                 "critical_alerts": [{"application": key, "violations": item.get("target_violations", []),
                                      "scoring_complete": item["scoring_complete"]} for key, item in summaries.items()
                                     if policy.get(key, {}).get("critical") and item.get("target_violations")],
                 "scoring_complete": complete, "coverage_pct": 100 * sum(weights[key] for key in scored),
                 "weight_source": "configured_workload" if application_shares is not None else "equal_application",
                 "missing_measurements": {key: item.get("missing_measurements", []) or ["sample_count"]
                                          for key, item in summaries.items() if not item["scoring_complete"]}}
    insufficient = total < minimum_transactions
    if application is not None and insufficient:
        score = None
    extra["scoring_complete"] = extra["scoring_complete"] and not insufficient
    extra["insufficient_sample"] = insufficient
    score = max(0.0, min(100.0, score)) if score is not None else None
    return {
        "score_model": SCORE_MODEL,
        "requests": total,
        "successes": len(successes),
        "failures": total - len(successes),
        "availability_pct": round(availability, 3),
        "p50_ms": round(percentile(latencies, 0.50), 3) if latencies else None,
        "p95_ms": round(p95, 3) if p95 is not None else None,
        "average_ms": round(statistics.mean(latencies), 3) if latencies else None,
        "latency_basis": "successful_operation_completion; UDP operation latency is delivered-packet mean RTT",
        "all_attempt_p95_ms": percentile([row.get("response_time_ms") for row in rows], .95),
        "cancelled_requests": sum(bool(row.get("cancelled")) for row in rows),
        "experience_score": round(score, 2) if score is not None else None,
        "experience": "Too few transactions" if score is None and insufficient else (
            "Incomplete evidence" if score is not None and score >= 75 and not extra["scoring_complete"] else score_label(score)),
        "bytes": sum(int(row["response_length"] or 0) for row in rows),
        **extra,
    }


def dem_summary(database_path, run_id=None, window_seconds=60, media_mode="strict", diagnosis=True, since=None, until=None):
    # Imported here: diagnosis builds on this module's summaries.
    from .diagnosis import application_detail, diagnose

    now = time.time()
    end = now if until is None else float(until)
    start = end - max(10, min(3600, float(window_seconds))) if since is None else float(since)
    if not math.isfinite(start) or not math.isfinite(end) or start >= end or end > now + .1 or end - start > 86400:
        raise ValueError("DEM requires a finite past interval, start < end, at most 24 hours.")
    window_seconds = end - start
    rows = query_transactions(database_path, start, run_id=run_id, limit=50000, until=end)
    contract, mixed = scoring_contract(database_path, run_id, start, end)
    policy = contract["policy"] if contract else load_policy(database_path)
    media_mode = (contract or {}).get("media_mode") or media_mode
    shares = contract["manifest"]["application_shares"] if contract else None
    overall = summarize_rows(rows, policy=policy, window_seconds=window_seconds, application_shares=shares)
    reasons = []
    if len(rows) >= 50000:
        reasons.append("row_limit")
    if mixed:
        reasons.append("configuration_changed_in_window")
        overall["experience_score"] = None
    if contract and contract.get("dropped_transactions", 0):
        reasons.append("dropped_transactions")
    if reasons:
        overall["scoring_complete"] = False
        overall["experience"] = "Incomplete evidence"

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

    for app in shares or {}:
        by_app[app]  # Preserve absent expected applications as unknown.
    app_summary = {
        key: summarize_rows(items, application=key, policy=policy, window_seconds=window_seconds)
        for key, items in sorted(by_app.items())
    }
    for key, item in app_summary.items():
        item["class"] = APPLICATIONS.get(key, {}).get("class", "interactive")
        item["configured_share"] = (shares or {}).get(key)
        item["weight"] = (overall.get("application_contributions", {}).get(key) or {}).get("weight")
    if diagnosis:
        for key, items in by_app.items():
            app_summary[key].update(application_detail(key, items, policy))
    persona_summary = {
        key: summarize_rows(items, policy=policy, window_seconds=window_seconds, scope="subset",
                            application_shares=(contract or {}).get("manifest", {}).get("persona_applications", {}).get(key))
        for key, items in sorted(by_persona.items())
    }
    endpoint_summary = {}
    for key, items in by_endpoint.items():
        if key == "unknown":
            continue
        summary = summarize_rows(items, policy=policy, window_seconds=window_seconds, scope="subset",
                                 application_shares=(contract or {}).get("manifest", {}).get("persona_applications", {}).get(endpoint_persona.get(key)))
        summary["endpoint_id"] = key
        summary["persona"] = endpoint_persona.get(key, "unknown")
        endpoint_summary[key] = summary

    if reasons:
        for group in (app_summary, persona_summary, endpoint_summary):
            for item in group.values():
                item["scoring_complete"] = False
                item["evidence_issues"] = list(reasons)
                item["experience"] = "Incomplete evidence"

    if rows:
        duration = float(window_seconds)
        rps = len(rows) / duration
        failures_per_second = sum(1 for row in rows if not row["success"]) / duration
    else:
        rps = 0.0
        failures_per_second = 0.0

    result = {
        "truncated": len(rows) >= 50000,
        "timestamp": now, "run_id": run_id, "since": start, "until": end,
        "evidence_issues": reasons, "policy_source": "run_snapshot" if contract else "current_policy_regrade",
        "score_model": SCORE_MODEL, "regraded": not bool(contract), "contract_epoch": (contract or {}).get("contract_epoch"),
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
        for app, item in app_summary.items():
            if item.get("target_violations") and item.get("scoring_complete"):
                result["diagnosis"]["findings"].append({
                    "id": "target_violation", "severity": "warn", "title": f"{APPLICATIONS.get(app, {}).get('label', app)} misses configured targets",
                    "detail": ", ".join(item["target_violations"]) + f"; measured utility {item['experience_score']}/100.",
                    "applications": [app], "affected": item["requests"], "by_egress": {"unknown": item["requests"]},
                    "evidence": {"targets": item["experience_targets"], "quality": item["quality"],
                                 "components": item["score_components"]}})
        result["interactive_p95_ms"] = result["diagnosis"]["interactive_p95_ms"]
    return result
