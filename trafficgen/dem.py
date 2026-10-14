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
            transfers = [row for row in rows if (row.get(field) or 0) >= 65536 or
                         (row.get("transfer_direction") == direction and (row.get("expected_bytes") or 0) >= 65536)]
            if not transfers:
                missing.append(f"sustained_{direction}")
            else:
                rate = sum(row.get(field) or 0 for row in transfers if row["success"]) * 8 / (window_seconds * 1000000)
                evidence[f"sustained_{direction}_mbps"] = round(rate, 3)
                # A missed explicit minimum cannot label the application good.
                components[f"sustained_{direction}"] = 100.0 if rate >= sustained_minimum else 100 * rate / sustained_minimum
        minimum = targets[f"min_{direction}_mbps"]
        if minimum <= 0:
            continue
        transfers = [row for row in rows if (row.get(field) or 0) >= 65536 or
                         (row.get("transfer_direction") == direction and (row.get("expected_bytes") or 0) >= 65536)]
        known = [row for row in transfers if row.get("response_time_ms") and row["response_time_ms"] > 0]
        if not known or len(known) != len(transfers):
            missing.append(f"throughput_{direction}")
        if known:
            rates = [row[field] * 8 / (row["response_time_ms"] * 1000) if row["success"] else 0 for row in known]
            evidence[f"transfer_{direction}_p05_mbps"] = round(percentile(rates, .05), 3)
            components[f"throughput_{direction}"] = min(100.0, 100 * percentile(rates, .05) / minimum)
    if bursts:
        import json
        from .packet_metrics import merge, percentile as packet_percentile
        for column, metric in (("packet_rtt_histogram", "packet_rtt"), ("packet_variation_histogram", "packet_rtt_variation")):
            histograms = [json.loads(row[column]) if isinstance(row.get(column), str) else row.get(column) for row in bursts]
            if all(item is not None for item in histograms):
                combined = merge(histograms)
                evidence[metric + "_histogram"] = combined
                evidence[metric + "_p95_upper_ms"] = packet_percentile(combined, .95)
        evidence["packets_late"] = sum(row.get("packets_late") or 0 for row in bursts)
        evidence["packets_missing_at_collection_end"] = sum(row.get("packets_lost") or 0 for row in bursts)
        rtts = evidence.get("packet_rtt_histogram")
        if rtts and all(row.get("packets_late") is not None for row in bursts):
            received = sum(rtts.values())
            late = evidence["packets_late"]
            components["deadline"] = (100 * (received - late) / received if targets.get("hard_deadline") else
                sum(count * target_credit(float(bound), targets["deadline_ms"]) for bound, count in rtts.items()) / received)
            evidence["deadline_misses"] = late
            evidence["deadline_basis"] = "received packets; histogram upper bounds for elastic credit"
        evidence["packet_loss_basis"] = "not received before bounded collection expiry; physical loss is not distinguishable from later arrival"
        if evidence.get("packet_rtt_variation_p95_upper_ms") is not None and targets["jitter_max_ms"] > 0:
            evidence["udp_rtt_variation_p95_ms"] = evidence["packet_rtt_variation_p95_upper_ms"]
            components["udp_rtt_variation"] = target_credit(evidence["udp_rtt_variation_p95_ms"], targets["jitter_max_ms"])
    return components, evidence, missing


def confirmed_miss_rate(misses, trials, allowed):
    """One-sided 95% Wilson lower bound, with at least three independent operations.

    This is evidence of a target violation, not proof of a cause. Packet samples
    within a burst are correlated; ordinary latency inference uses operations.
    """
    if misses < 3 or not trials:
        return False
    z = 1.6448536269514722
    rate = misses / trials
    lower = (rate + z*z/(2*trials) - z*math.sqrt(rate*(1-rate)/trials + z*z/(4*trials*trials))) / (1 + z*z/trials)
    return lower > allowed + 1e-12


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
        if scope != targets.get("sustained_scope", "application") and scope != "application":
            # Site-wide sustained targets are not per-endpoint/persona/egress requirements.
            targets.update(min_sustained_down_mbps=0, min_sustained_up_mbps=0)
        scoped_target = targets.get("sustained_scope", "application")
        components, evidence, missing = quality_components(rows, targets, window_seconds)
        if scope == "application" and scoped_target != "application":
            field = {"endpoint": "endpoint_id", "persona": "persona", "egress": "egress"}[scoped_target]
            groups = defaultdict(list)
            for row in rows:
                groups[row.get(field)].append(row)
            if None in groups:
                missing.append("throughput_scope_identity")
            member_quality = [quality_components(items, targets, window_seconds) for items in groups.values()]
            evidence["sustained_scope"] = scoped_target
            evidence["scope_members"] = len(groups)
            for direction in ("down", "up"):
                key = "sustained_" + direction
                if targets.get("min_" + key + "_mbps", 0):
                    values = [item[0][key] for item in member_quality if key in item[0]]
                    if len(values) != len(groups):
                        missing.append(key + "_scope_coverage")
                    components[key] = statistics.mean(values) if values else 0
                    evidence[key + "_mbps"] = min((item[1].get(key + "_mbps", 0) for item in member_quality), default=0)
                    evidence[key + "_basis"] = "minimum member goodput for compliance; mean member utility for score"
        quality_score = min(components.values(), default=100)
        loss_accounted = sum(not row["success"] and (row.get("packets_sent") or 0) > 0
                             and row.get("packets_lost") == row["packets_sent"] for row in rows)
        transport_pct = (len(successes) + loss_accounted) * 100 / total
        components["reliability"] = reliability_score(transport_pct, targets.get("success_min_pct", 100))
        score = quality_score * components["reliability"] / 100
        latency_p95 = evidence.get("packet_rtt_p95_upper_ms", p95)
        compliance = {"success": availability >= targets.get("success_min_pct", 100),
                      "deadline": (evidence.get("deadline_misses", 0) == 0 if targets.get("hard_deadline")
                                   else latency_p95 <= targets["deadline_ms"] if latency_p95 is not None else None)}
        if "udp_loss_pct" in evidence:
            compliance["loss"] = evidence["udp_loss_pct"] <= targets["loss_max_pct"]
        if "udp_rtt_variation_p95_ms" in evidence:
            compliance["jitter"] = evidence["udp_rtt_variation_p95_ms"] <= targets["jitter_max_ms"]
        for direction in ("down", "up"):
            for kind, metric, target in (("throughput", "transfer_" + direction + "_p05_mbps", "min_" + direction + "_mbps"),
                                         ("sustained", "sustained_" + direction + "_mbps", "min_sustained_" + direction + "_mbps")):
                if targets.get(target, 0):
                    compliance[kind + "_" + direction] = evidence.get(metric) is not None and evidence[metric] >= targets[target]
        observed = [key for key, met in compliance.items() if met is False]
        confirmed = []
        for key in observed:
            if key == "success":
                supported = confirmed_miss_rate(total - len(successes), total, 1 - targets.get("success_min_pct", 100) / 100)
            elif key == "deadline" and not targets.get("hard_deadline"):
                # A burst is one observation; its packets are not independent trials.
                misses = sum(row["response_time_ms"] > targets["deadline_ms"] for row in successes if row.get("response_time_ms") is not None)
                supported = confirmed_miss_rate(misses, len(latencies), .05)
            else:
                supported = bool(targets.get("hard_deadline") and key == "deadline") or (total >= minimum_transactions and not missing)
            if supported:
                confirmed.append(key)
        severity = "outage" if confirmed and not successes and total >= 3 else "violation" if confirmed else "observation" if observed else "none"
        extra = {"criticality": targets.get("criticality", "critical" if targets["critical"] else "standard"),
                 "criticality_weight": criticality_weight(targets), "compliance": compliance,
                 "target_violations": confirmed, "target_observations": observed,
                 "alert_severity": severity, "compliance_method": "configured P95; one-sided Wilson 95%, minimum 3 misses; explicit hard deadlines zero tolerance",
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
        scored = {key: item for key, item in summaries.items() if item["experience_score"] is not None and item["scoring_complete"]}
        coverage = sum(weights[key] for key in scored)
        observed_points = sum(weights[key] * item["experience_score"] for key, item in scored.items())
        score = observed_points / coverage if coverage else None
        limiting = max(scored, key=lambda key: weights[key] * (100 - scored[key]["experience_score"])) if scored else None
        contributions = {key: {"configured_share": shares[key], "weight": weights[key],
                         "score": item["experience_score"], "criticality_weight": criticality_weight(policy.get(key, {})),
                         "lost_points": weights[key] * (100 - item["experience_score"]) if item["experience_score"] is not None else None,
                         "target_violations": item.get("target_violations", []), "target_observations": item.get("target_observations", []),
                         "alert_severity": item.get("alert_severity", "none"), "requests": item["requests"],
                         "scoring_complete": item["scoring_complete"]} for key, item in summaries.items()}
        extra = {"limiting_application": limiting, "application_contributions": contributions,
                 "critical_applications": sorted(key for key in summaries if policy.get(key, {}).get("critical")),
                 "critical_alerts": [{"application": key, "violations": item.get("target_violations", []),
                                      "severity": item.get("alert_severity"), "observations": item.get("target_observations", []),
                                      "scoring_complete": item["scoring_complete"]} for key, item in summaries.items()
                                     if policy.get(key, {}).get("critical") and item.get("target_observations")],
                 "scoring_complete": complete, "coverage_pct": round(100 * coverage, 3),
                 "score_range": {"minimum": round(observed_points, 2), "maximum": round(observed_points + 100 * (1 - coverage), 2)},
                 "score_status": "complete" if complete and total >= minimum_transactions else "provisional",
                 "business_acceptance": ("fail" if any(policy.get(key, {}).get("critical") and item.get("target_violations") for key, item in summaries.items())
                                         else "unknown" if not complete or total < minimum_transactions or any(item.get("target_observations") for item in summaries.values()) else "pass"),
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


def dem_summary(database_path, run_id=None, window_seconds=60, media_mode="strict", diagnosis=True, since=None, until=None, intervals=None):
    # Imported here: diagnosis builds on this module's summaries.
    from .diagnosis import application_detail, diagnose

    now = time.time()
    end = now if until is None else float(until)
    start = end - max(5, min(3600, float(window_seconds))) if since is None else float(since)
    if not math.isfinite(start) or not math.isfinite(end) or start >= end or end > now + .1 or end - start > 86400:
        raise ValueError("DEM requires a finite past interval, start < end, at most 24 hours.")
    if intervals is not None:
        if not isinstance(intervals, list) or not 1 <= len(intervals) <= 64:
            raise ValueError("DEM intervals must contain 1 to 64 active segments.")
        previous = start
        for segment in intervals:
            if not isinstance(segment, (list, tuple)) or len(segment) != 2:
                raise ValueError("Each DEM interval needs start and end.")
            left, right = segment
            if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in segment):
                raise ValueError("DEM interval bounds must be finite numbers.")
            if left < previous or left >= right or right > end:
                raise ValueError("DEM intervals must be ordered, disjoint and inside the requested bounds.")
            previous = right
    window_seconds = sum(right - left for left, right in intervals) if intervals else end - start
    rows = query_transactions(database_path, start, run_id=run_id, limit=50000, until=end, intervals=intervals)
    contract, mixed = scoring_contract(database_path, run_id, start, end)
    policy = contract["policy"] if contract else load_policy(database_path)
    media_mode = (contract or {}).get("media_mode") or media_mode
    shares = contract["manifest"]["application_shares"] if contract else None
    overall = summarize_rows(rows, policy=policy, window_seconds=window_seconds, application_shares=shares)
    from .legacy_score import legacy_summary
    overall["legacy_score"] = legacy_summary(rows, policy=policy, window_seconds=window_seconds)["experience_score"]
    overall["legacy_model"] = "legacy-v1-f0aea4c-shadow"
    from .database import operation_evidence
    lifecycle = operation_evidence(database_path, run_id, start, end)
    if intervals:
        evidence = [operation_evidence(database_path, run_id, left, right) for left, right in intervals]
        lifecycle = dict(evidence[-1])
        lifecycle["events"] = {kind: sum(item["events"].get(kind, 0) for item in evidence)
                               for kind in {kind for item in evidence for kind in item["events"]}}
        lifecycle["clock_discontinuities"] = sum(item["clock_discontinuities"] for item in evidence)
    reasons = []
    if lifecycle["overdue_at_end"]:
        reasons.append("outstanding_overdue_operations")
    if lifecycle["clock_discontinuities"]:
        reasons.append("clock_discontinuity")
    if len(rows) >= 50000:
        reasons.append("row_limit")
    if mixed:
        reasons.append("configuration_changed_in_window")
        overall["experience_score"] = None
    if contract and contract.get("dropped_transactions", 0):
        reasons.append("dropped_transactions")
    if reasons:
        overall["score_status"] = "provisional"
        overall["business_acceptance"] = "unknown" if overall.get("business_acceptance") != "fail" else "fail"
        if mixed or any(reason in reasons for reason in ("clock_discontinuity", "row_limit", "dropped_transactions")):
            overall["business_acceptance"] = "unknown"
            overall["critical_alerts"] = []
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
        item["sampling_guidance"] = (contract or {}).get("manifest", {}).get("sampling_guidance", {}).get(key)
        item["weight"] = (overall.get("application_contributions", {}).get(key) or {}).get("weight")
    if diagnosis:
        for key, items in by_app.items():
            app_summary[key].update(application_detail(key, items, policy))
    persona_summary = {
        key: summarize_rows(items, policy=policy, window_seconds=window_seconds, scope="persona",
                            application_shares=(contract or {}).get("manifest", {}).get("persona_applications", {}).get(key))
        for key, items in sorted(by_persona.items())
    }
    endpoint_summary = {}
    for key, items in by_endpoint.items():
        if key == "unknown":
            continue
        summary = summarize_rows(items, policy=policy, window_seconds=window_seconds, scope="endpoint",
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
        "operation_lifecycle": lifecycle,
        "at_risk": bool(lifecycle["overdue_at_end"]),
        "at_risk_reason": "Operations have exceeded their target and are still pending" if lifecycle["overdue_at_end"] else None,
        "truncated": len(rows) >= 50000,
        "timestamp": now, "run_id": run_id, "since": start, "until": end,
        "evidence_issues": reasons, "policy_source": "run_snapshot" if contract else "current_policy_regrade",
        "score_model": SCORE_MODEL, "regraded": not bool(contract), "contract_epoch": (contract or {}).get("contract_epoch"),
        "window_seconds": window_seconds, "intervals": intervals,
        **overall,
        "requests_per_second": round(rps, 3),
        "failures_per_second": round(failures_per_second, 3),
        "applications": app_summary,
        "personas": persona_summary,
        "endpoints": endpoint_summary,
    }
    import json
    transitions = []
    for row in rows:
        packet_transitions = row.get("packet_transitions")
        if packet_transitions:
            for item in json.loads(packet_transitions) if isinstance(packet_transitions, str) else packet_transitions:
                if start <= item["latest_at"] < end and (not intervals or any(left <= item["latest_at"] < right for left, right in intervals)):
                    transitions.append(dict(item, application=row["application"], endpoint_id=row.get("endpoint_id")))
    result["packet_egress_transitions"] = transitions[-1000:]
    result["packet_egress_transitions_truncated"] = len(transitions) > 1000
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
        for finding in result["diagnosis"]["findings"]:
            finding["observation"] = {"run_id": run_id, "since": start, "until": end,
                                      "path": (contract or {}).get("traffic_path")}
            finding["confidence"] = "observed symptom"
            finding["causality"] = "cause requires independent path/target evidence"
        result["interactive_p95_ms"] = result["diagnosis"]["interactive_p95_ms"]
    return result
