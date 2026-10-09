"""Explain synthetic experience: why transactions fail, where their time goes and
which appliance egress address (WAN) carried them.

Everything here is a pure function of recorded transactions so the rules stay
transparent and testable. Findings describe symptoms seen by the simulator; NetEm
correlates them with per-WAN path measurements to name the bottleneck.
"""
from __future__ import annotations

import re
import statistics
import time
from collections import Counter, defaultdict

from .dem import percentile, summarize_rows
from .profiles import APPLICATIONS, MEDIA_MODES
from .wire import OBSERVED_SOURCE_HEADER, normalize_address

UNKNOWN_EGRESS = "unknown"
# Payloads at least this large are measured as transfers (throughput, transfer share).
BULK_BYTES = 64 * 1024
# Rules need this many timed samples before judging an application.
MIN_SAMPLES = 5
BANDWIDTH_SHARE = 0.6
# Short window showing where traffic goes right now, e.g. to time SD-WAN steering.
RECENT_SECONDS = 10

CAUSES = {
    "connect_timeout": "Connect timeout",
    "read_timeout": "Read timeout",
    "connection_error": "Connection reset or refused",
    "http_status": "HTTP error status",
    "redirect": "Unexpected redirect",
    "media_loss": "Media packet loss",
    "media_no_reply": "No media replies",
    "other": "Other",
}
SEVERITY_ORDER = {"bad": 0, "warn": 1, "info": 2}


class MediaFailure(RuntimeError):
    """A voice/video burst that failed delivery, tagged with its cause."""

    def __init__(self, cause: str, message: str):
        super().__init__(message)
        self.cause = cause


def _chain(exc):
    chain = []
    while exc is not None and len(chain) < 8 and all(exc is not item for item in chain):
        chain.append(exc)
        exc = exc.__cause__ or exc.__context__ or getattr(exc, "reason", None)
        if not isinstance(exc, BaseException):
            exc = None
    return chain


def classify_error_text(text) -> str:
    """Classify a stored error message; used for rows recorded before causes existed."""
    message = str(text or "").lower()
    loss = re.search(r"udp packet loss:\s*(\d+)\s*/\s*(\d+)", message)
    if loss:
        return "media_no_reply" if loss.group(1) == loss.group(2) else "media_loss"
    if "no udp replies" in message:
        return "media_no_reply"
    if "connect timeout" in message or "connecttimeout" in message:
        return "connect_timeout"
    if "read timed out" in message or "read timeout" in message or "timed out" in message:
        return "read_timeout"
    if any(word in message for word in ("refused", "reset", "aborted", "broken pipe",
                                        "remote end closed", "connection broken", "failed to establish")):
        return "connection_error"
    if "redirect" in message:
        return "redirect"
    if re.search(r"\b[45]\d\d\b.*\berror\b|\bstatus code\b", message):
        return "http_status"
    return "other"


def classify_failure(exc):
    """Name why a transaction failed. Locust unwraps connection errors to their
    lowest-level cause, so walk the chain and decide by exact class name."""
    if exc is None:
        return None
    for item in _chain(exc):
        if isinstance(item, MediaFailure):
            return item.cause
        name = type(item).__name__
        if name in ("ConnectTimeout", "ConnectTimeoutError"):
            return "connect_timeout"
        if name in ("ReadTimeout", "ReadTimeoutError", "TimeoutError", "timeout"):
            return "read_timeout"
        if name == "HTTPError" and getattr(item, "response", None) is not None:
            return "http_status"
        if name in ("ConnectionRefusedError", "ConnectionResetError", "ConnectionAbortedError",
                    "BrokenPipeError", "NewConnectionError", "ProtocolError", "ChunkedEncodingError",
                    "RemoteDisconnected", "IncompleteRead"):
            return "connection_error"
    chain = _chain(exc)
    cause = classify_error_text(" ".join(str(item) for item in chain))
    if cause == "other" and any(type(item).__name__ == "ConnectionError" for item in chain):
        return "connection_error"
    return cause


def response_details(response, response_time_ms):
    """Split an HTTP transaction into waiting (request sent until headers arrive,
    including any upload) and transferring the response body."""
    details = {"wait_ms": None, "transfer_ms": None, "bytes_up": None, "status_code": None, "egress": None}
    if response is None:
        return details
    status = getattr(response, "status_code", None)
    details["status_code"] = status if isinstance(status, int) and status > 0 else None
    request = getattr(response, "request", None)
    if request is not None:
        body = getattr(request, "body", None)
        if isinstance(body, (bytes, bytearray)):
            details["bytes_up"] = len(body)
        elif isinstance(body, str):
            details["bytes_up"] = len(body.encode())
        elif body is None:
            details["bytes_up"] = 0
    headers = getattr(response, "headers", None)
    if headers is not None:
        details["egress"] = normalize_address(headers.get(OBSERVED_SOURCE_HEADER))
    elapsed = getattr(response, "elapsed", None)
    if details["status_code"] and elapsed is not None and response_time_ms is not None:
        total = float(response_time_ms)
        wait = max(0.0, min(total, elapsed.total_seconds() * 1000))
        details["wait_ms"] = round(wait, 3)
        details["transfer_ms"] = round(total - wait, 3)
    return details


def row_cause(row):
    return row.get("cause") or classify_error_text(row.get("error"))


def _label(app):
    return APPLICATIONS.get(app, {}).get("label", str(app).replace("_", " ").title())


def _class(app):
    return APPLICATIONS.get(app, {}).get("class", "interactive")


def _ms(value):
    return f"{value:.0f} ms" if value < 1000 else f"{value / 1000:.1f} s"


def _is_upload(row):
    return (row.get("bytes_up") or 0) >= BULK_BYTES


def _payload_ms(row):
    # Upload bodies are sent before the response starts, so they sit inside the wait.
    return row["wait_ms"] if _is_upload(row) else row["transfer_ms"]


def _rate_mbps(row):
    if _is_upload(row):
        return row["bytes_up"] * 8 / (row["wait_ms"] * 1000) if row["wait_ms"] > 0 else None
    size = row.get("response_length") or 0
    if size >= BULK_BYTES and row["transfer_ms"] > 0:
        return size * 8 / (row["transfer_ms"] * 1000)
    return None


def _round(value, digits=1):
    return round(value, digits) if value is not None else None


def _media_fails(row, tolerance_pct):
    sent = row.get("packets_sent") or 0
    lost = row.get("packets_lost") or 0
    if row_cause(row) not in (None, "media_loss", "media_no_reply"):
        return not row["success"]
    if not sent or lost >= sent:
        return True
    return lost * 100.0 / sent > tolerance_pct


def _timed(rows):
    return [row for row in rows if row["success"] and row.get("wait_ms") is not None
            and row.get("transfer_ms") is not None and row.get("response_time_ms") is not None]


def application_detail(app, rows):
    """Per-application causes, timing split, throughput and media loss."""
    failures = Counter(row_cause(row) for row in rows if not row["success"])
    timed = _timed(rows)
    detail = {
        "class": _class(app),
        "causes": dict(failures.most_common()),
        "top_cause": failures.most_common(1)[0][0] if failures else None,
        "p95_wait_ms": _round(percentile([row["wait_ms"] for row in timed], 0.95)),
        "p95_transfer_ms": _round(percentile([row["transfer_ms"] for row in timed], 0.95)),
        "median_down_mbps": None,
        "median_up_mbps": None,
        "transfer_share": None,
        "media": None,
    }
    downs = [rate for row in timed if not _is_upload(row) and (rate := _rate_mbps(row)) is not None]
    ups = [rate for row in timed if _is_upload(row) and (rate := _rate_mbps(row)) is not None]
    detail["median_down_mbps"] = _round(statistics.median(downs), 2) if downs else None
    detail["median_up_mbps"] = _round(statistics.median(ups), 2) if ups else None
    total = sum(float(row["response_time_ms"]) for row in timed)
    if total > 0:
        detail["transfer_share"] = round(sum(_payload_ms(row) for row in timed) / total, 3)

    bursts = [row for row in rows if row.get("packets_sent")]
    if bursts:
        sent = sum(row["packets_sent"] for row in bursts)
        lost = sum(row.get("packets_lost") or 0 for row in bursts)
        detail["media"] = {
            "bursts": len(bursts),
            "packets_sent": sent,
            "packets_lost": lost,
            "loss_pct": round(lost * 100.0 / sent, 3) if sent else None,
            "bursts_with_loss": sum(1 for row in bursts if 0 < (row.get("packets_lost") or 0) < row["packets_sent"]),
            "no_reply": sum(1 for row in bursts if (row.get("packets_lost") or 0) >= row["packets_sent"]),
            "fail_by_mode": {
                mode: sum(1 for row in bursts if _media_fails(row, item["tolerance_pct"].get(app, 0.0)))
                for mode, item in MEDIA_MODES.items()
            },
        }
    return detail


def egress_breakdown(rows):
    """Experience per appliance egress address; no-reply failures stay 'unknown'."""
    groups = defaultdict(list)
    for row in rows:
        groups[row.get("egress") or UNKNOWN_EGRESS].append(row)
    result = {}
    for address, items in sorted(groups.items()):
        summary = summarize_rows(items)
        apps = defaultdict(lambda: [0, 0, []])
        for row in items:
            counts = apps[row.get("application") or "other"]
            counts[0] += 1
            counts[1] += 0 if row["success"] else 1
            if row["success"] and row.get("response_time_ms") is not None:
                counts[2].append(row["response_time_ms"])
        result[address] = {
            **{key: summary[key] for key in ("requests", "successes", "failures", "availability_pct",
                                             "p95_ms", "experience_score", "experience")},
            "causes": dict(Counter(row_cause(row) for row in items if not row["success"]).most_common()),
            "applications": {
                app: {"requests": total, "failures": failed,
                      "availability_pct": round((total - failed) * 100.0 / total, 3),
                      "p95_ms": _round(percentile(times, 0.95))}
                for app, (total, failed, times) in sorted(apps.items())
            },
        }
    return result


def recent_egress(rows, now=None, seconds=RECENT_SECONDS):
    """Transactions per egress address and application over the last few seconds."""
    since = (now or time.time()) - seconds
    result = defaultdict(lambda: defaultdict(lambda: {"requests": 0, "failures": 0}))
    for row in rows:
        if (row.get("timestamp") or 0) < since:
            continue
        counts = result[row.get("egress") or UNKNOWN_EGRESS][row.get("application") or "other"]
        counts["requests"] += 1
        counts["failures"] += 0 if row["success"] else 1
    return {"window_seconds": seconds,
            "egress": {address: dict(apps) for address, apps in sorted(result.items())}}


def _by_egress(rows):
    return dict(Counter(row.get("egress") or UNKNOWN_EGRESS for row in rows).most_common())


def _finding(finding_id, severity, title, detail, rows, applications, **extra):
    return {"id": finding_id, "severity": severity, "title": title, "detail": detail,
            "applications": applications, "affected": len(rows), "by_egress": _by_egress(rows), **extra}


def _media_findings(rows, applications, media_mode):
    findings = []
    for app, detail in applications.items():
        media = detail.get("media")
        if not media:
            continue
        app_rows = [row for row in rows if row.get("application") == app and row.get("packets_sent")]
        mode_label = MEDIA_MODES[media_mode]["label"].lower()
        failing = media["fail_by_mode"].get(media_mode, 0)
        lossy = [row for row in app_rows if 0 < (row.get("packets_lost") or 0) < row["packets_sent"]]
        if lossy:
            availability = (media["bursts"] - failing) * 100.0 / media["bursts"]
            findings.append(_finding(
                "media_loss", "bad" if availability < 95 else ("warn" if failing else "info"),
                f"{_label(app)}: {len(lossy)} of {media['bursts']} bursts lost packets",
                f"Packet loss {media['loss_pct']:.2f}% across {media['packets_sent']} packets. "
                f"In {mode_label} mode {failing} bursts fail (strict {media['fail_by_mode']['strict']}, "
                f"realistic {media['fail_by_mode']['realistic']}).",
                lossy, [app], media_mode=media_mode, fail_by_mode=media["fail_by_mode"],
                loss_pct=media["loss_pct"]))
        silent = [row for row in app_rows if (row.get("packets_lost") or 0) >= row["packets_sent"]]
        if silent:
            findings.append(_finding(
                "media_no_reply", "bad",
                f"{_label(app)}: {len(silent)} of {media['bursts']} bursts got no reply",
                "No echo came back within 0.5 s. Either the path dropped every packet, UDP replies are "
                "blocked, or the target answers from a different address than the one contacted "
                "(the appliance then drops the reply).",
                silent, [app]))
    return findings


def _failure_findings(rows):
    failed = [row for row in rows if not row["success"] and row.get("request_type") != "UDP"]
    by_cause = defaultdict(list)
    for row in failed:
        by_cause[row_cause(row)].append(row)
    total = max(1, len(rows))
    findings = []

    timeouts = by_cause["connect_timeout"] + by_cause["read_timeout"]
    if timeouts:
        parts = []
        if by_cause["connect_timeout"]:
            parts.append(f"{len(by_cause['connect_timeout'])} connect (no answer within 5 s: lost SYNs, "
                         "a blocked or failed path)")
        if by_cause["read_timeout"]:
            parts.append(f"{len(by_cause['read_timeout'])} read (no data for 10 s: a stalled transfer)")
        findings.append(_finding(
            "timeouts", "bad" if len(timeouts) * 100 / total >= 2 else "warn",
            f"{len(timeouts)} requests timed out", "; ".join(parts) + ".",
            timeouts, sorted({row.get("application") or "other" for row in timeouts})))

    resets = by_cause["connection_error"]
    if resets:
        findings.append(_finding(
            "connection_errors", "bad" if len(resets) * 100 / total >= 2 else "warn",
            f"{len(resets)} connections reset or refused",
            "Something on the path, the appliance or the target closed or refused connections.",
            resets, sorted({row.get("application") or "other" for row in resets})))

    statuses = by_cause["http_status"] + by_cause["redirect"]
    if statuses:
        codes = Counter(row.get("status_code") or "redirect" for row in statuses)
        findings.append(_finding(
            "http_errors", "bad" if len(statuses) * 100 / total >= 2 else "warn",
            f"{len(statuses)} requests got HTTP errors",
            "Status " + ", ".join(f"{code} ×{count}" for code, count in codes.most_common()) +
            ". These come from the appliance (for example a block page) or the target, not from network quality.",
            statuses, sorted({row.get("application") or "other" for row in statuses}),
            status_codes={str(code): count for code, count in codes.items()}))
    return findings


def _timing_findings(rows, applications):
    findings = []
    by_app = defaultdict(list)
    for row in _timed(rows):
        by_app[row.get("application") or "other"].append(row)

    # Bulk transfers whose time is dominated by moving data: bandwidth-limited.
    limited = defaultdict(list)
    for app, items in by_app.items():
        detail = applications.get(app) or {}
        profile = APPLICATIONS.get(app, {})
        if detail.get("class") != "bulk" or len(items) < MIN_SAMPLES:
            continue
        p95 = percentile([row["response_time_ms"] for row in items], 0.95)
        if p95 > profile.get("latency_good_ms", 400) and (detail.get("transfer_share") or 0) >= BANDWIDTH_SHARE:
            uploads = sum(_payload_ms(row) for row in items if _is_upload(row))
            direction = "upload" if uploads * 2 > sum(_payload_ms(row) for row in items) else "download"
            limited[direction].append((app, p95, profile.get("latency_poor_ms", 1800), items))
    for direction, entries in limited.items():
        items = [row for entry in entries for row in entry[3]]
        rates = [rate for row in items if (rate := _rate_mbps(row)) is not None]
        share = sum(_payload_ms(row) for row in items) / max(1e-9, sum(row["response_time_ms"] for row in items))
        worst = max(entries, key=lambda entry: entry[1])
        apps = [entry[0] for entry in entries]
        by_egress = {}
        for address, group in sorted(_group_by_egress(items).items()):
            group_rates = [rate for row in group if (rate := _rate_mbps(row)) is not None]
            by_egress[address] = {
                "transfers": len(group),
                "p95_ms": _round(percentile([row["response_time_ms"] for row in group], 0.95)),
                "median_mbps": _round(statistics.median(group_rates), 2) if group_rates else None,
            }
        findings.append({
            "id": "bandwidth_bound", "severity": "bad" if any(p95 > poor for _, p95, poor, _ in entries) else "warn",
            "title": f"{'Uploads' if direction == 'upload' else 'Downloads'} limited by bandwidth: "
                     + ", ".join(_label(app) for app in apps),
            "detail": f"{_label(worst[0])} P95 {_ms(worst[1])}; {share:.0%} of the time is spent moving data"
                      + (f" at a median {statistics.median(rates):.1f} Mbit/s per transfer." if rates else "."),
            "applications": apps, "direction": direction, "affected": len(items), "by_egress": by_egress,
        })

    # Interactive requests waiting long for the first byte: delay or queueing before the response.
    waiting = []
    for app, items in by_app.items():
        profile = APPLICATIONS.get(app, {})
        if _class(app) != "interactive" or len(items) < MIN_SAMPLES:
            continue
        p95_wait = percentile([row["wait_ms"] for row in items], 0.95)
        if p95_wait > profile.get("latency_good_ms", 400):
            waiting.append((app, p95_wait, profile.get("latency_poor_ms", 1800), items))
    if waiting:
        items = [row for entry in waiting for row in entry[3]]
        worst = max(waiting, key=lambda entry: entry[1])
        by_egress = {
            address: {"requests": len(group),
                      "p95_wait_ms": _round(percentile([row["wait_ms"] for row in group], 0.95))}
            for address, group in sorted(_group_by_egress(items).items())
        }
        findings.append({
            "id": "slow_wait", "severity": "bad" if any(p95 > poor for _, p95, poor, _ in waiting) else "warn",
            "title": f"{', '.join(_label(app) for app, *_ in waiting)}: requests wait "
                     f"{_ms(worst[1])} (P95) before the response starts",
            "detail": "Waiting time is path delay, queueing behind other traffic, or a slow appliance or target. "
                      f"Moving the data itself takes {_ms(percentile([row['transfer_ms'] for row in items], 0.95))} (P95).",
            "applications": [app for app, *_ in waiting], "affected": len(items), "by_egress": by_egress,
        })
    return findings


def _group_by_egress(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row.get("egress") or UNKNOWN_EGRESS].append(row)
    return groups


TARGET_CAPABILITIES = ("media-echo", "observed-source")


def target_finding(info, simulator_version):
    """Explain an outdated controlled target, which cannot report the WAN (and before
    v0.2.1 did not echo media at all, so every voice/video burst fails)."""
    if not info or info.get("error") or set(TARGET_CAPABILITIES) <= set(info.get("capabilities") or ()):
        return None
    version = info.get("version")
    target_match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(version or ""))
    simulator_match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(simulator_version))
    older = bool(target_match and simulator_match
                 and tuple(map(int, target_match.groups())) < tuple(map(int, simulator_match.groups())))
    missing = sorted(set(TARGET_CAPABILITIES) - set(info.get("capabilities") or ()))
    return {
        "id": "target_outdated" if older else "target_incompatible", "severity": "warn",
        "title": (f"Controlled target is older than the simulator (v{simulator_version})" if older
                  else "Controlled target is missing required capabilities"),
        "detail": f"The target reports {version or 'no version'}; simulator version is {simulator_version}. "
                  f"Missing capabilities: {', '.join(missing)}. "
                  "observed-source is needed for per-WAN attribution; media-echo is needed for voice/video replies. "
                  "Verify /health from the simulator and update the target if required. Health is rechecked every 30 seconds.",
        "applications": [], "affected": 0, "by_egress": {}, "target": info,
    }


def diagnose(rows, applications, media_mode="strict", now=None):
    """Explain the window: failure causes, interactive response time, experience per
    egress address and ordered findings. `applications` is `application_detail` per app."""
    media_mode = media_mode if media_mode in MEDIA_MODES else "strict"
    interactive = [float(row["response_time_ms"]) for row in rows
                   if row["success"] and row.get("response_time_ms") is not None
                   and _class(row.get("application")) == "interactive"]
    findings = (_media_findings(rows, applications, media_mode) + _failure_findings(rows)
                + _timing_findings(rows, applications))
    findings.sort(key=lambda item: (SEVERITY_ORDER.get(item["severity"], 9), -item["affected"]))
    return {
        "media_mode": media_mode,
        "cause_labels": CAUSES,
        "interactive_p95_ms": _round(percentile(interactive, 0.95), 3),
        "causes": dict(Counter(row_cause(row) for row in rows if not row["success"]).most_common()),
        "egress": egress_breakdown(rows),
        "egress_recent": recent_egress(rows, now),
        "findings": findings,
    }
