"""Editable business experience targets; no independent probe traffic."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .config import _write_secret
from .profiles import APPLICATIONS

CRITICALITY_WEIGHTS = {"standard": 1.0, "important": 2.0, "critical": 4.0}
SCORE_MODEL = "workload-utility-v2"


def criticality_weight(targets):
    return float(targets.get("criticality_weight", CRITICALITY_WEIGHTS.get(
        targets.get("criticality"), 4.0 if targets.get("critical") else 1.0)))


def default_policy():
    return {name: {
        "critical": app["class"] != "bulk",
        "criticality": "critical" if app["class"] != "bulk" else "standard",
        "criticality_weight": 4.0 if app["class"] != "bulk" else 1.0,
        "hard_deadline": False,
        "success_min_pct": 99.0,
        "deadline_ms": float(app["latency_good_ms"]),
        "jitter_max_ms": 30.0 if app["class"] == "realtime" else 0.0,
        "loss_max_pct": 2.0 if name == "voice" else 1.0 if name == "video" else 0.0,
        "min_down_mbps": 0.0, "min_up_mbps": 0.0,
        "min_sustained_down_mbps": 0.0, "min_sustained_up_mbps": 0.0,
        "sustained_scope": "application",
    } for name, app in APPLICATIONS.items()}


def validate_policy(raw):
    if not isinstance(raw, dict) or set(raw) - set(APPLICATIONS):
        raise ValueError("Experience settings must name known applications.")
    policy = default_policy()
    for name, fields in raw.items():
        if not isinstance(fields, dict) or set(fields) - set(policy[name]):
            raise ValueError(f"Invalid experience settings for {name}.")
        for key, value in fields.items():
            if key in ("critical", "hard_deadline"):
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be true or false.")
            elif key == "sustained_scope":
                if value not in ("application", "endpoint", "persona", "egress"):
                    raise ValueError("Sustained throughput scope must be application, endpoint, persona or egress.")
            elif key == "criticality":
                if not isinstance(value, str) or value not in CRITICALITY_WEIGHTS:
                    raise ValueError("Criticality must be standard, important or critical.")
            else:
                if isinstance(value, bool):
                    raise ValueError(f"{name}: {key} must be a number, not a boolean.")
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    raise ValueError(f"{name}: {key} must be a number.") from None
                ceiling = 100 if key in ("loss_max_pct", "success_min_pct", "criticality_weight") else 600000 if key.endswith("ms") else 100000
                if not math.isfinite(value) or value < 0 or value > ceiling or (key == "deadline_ms" and value == 0):
                    raise ValueError(f"{name}: invalid {key}.")
                if key == "criticality_weight" and value == 0:
                    raise ValueError("Criticality weight must be positive.")
            policy[name][key] = value
        if "criticality" in fields:
            policy[name]["critical"] = fields["criticality"] == "critical"
        elif "critical" in fields:
            policy[name]["criticality"] = "critical" if fields["critical"] else "standard"
        if "criticality_weight" not in fields:
            policy[name]["criticality_weight"] = CRITICALITY_WEIGHTS[policy[name]["criticality"]]
    return policy


def policy_path(database_path):
    return Path(database_path).parent / "experience-policy.json"


def load_policy(database_path):
    path = policy_path(database_path)
    if not path.exists():
        return default_policy()
    # Invalid saved policy must not silently fall back to more lenient targets.
    return validate_policy(json.loads(path.read_text(encoding="utf-8")))


def save_policy(database_path, raw):
    policy = validate_policy(raw)
    _write_secret(policy_path(database_path), json.dumps(policy, sort_keys=True))
    return policy
