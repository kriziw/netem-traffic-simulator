"""Editable business experience targets; no independent probe traffic."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .config import _write_secret
from .profiles import APPLICATIONS


def default_policy():
    return {name: {
        "critical": app["class"] != "bulk",
        "deadline_ms": float(app["latency_good_ms"]),
        "jitter_max_ms": 30.0 if app["class"] == "realtime" else 0.0,
        "loss_max_pct": 2.0 if name == "voice" else 1.0 if name == "video" else 0.0,
        "min_down_mbps": 0.0, "min_up_mbps": 0.0,
        "min_sustained_down_mbps": 0.0, "min_sustained_up_mbps": 0.0,
    } for name, app in APPLICATIONS.items()}


def validate_policy(raw):
    if not isinstance(raw, dict) or set(raw) - set(APPLICATIONS):
        raise ValueError("Experience settings must name known applications.")
    policy = default_policy()
    for name, fields in raw.items():
        if not isinstance(fields, dict) or set(fields) - set(policy[name]):
            raise ValueError(f"Invalid experience settings for {name}.")
        for key, value in fields.items():
            if key == "critical":
                if not isinstance(value, bool):
                    raise ValueError("Critical must be true or false.")
            else:
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    raise ValueError(f"{name}: {key} must be a number.") from None
                ceiling = 100 if key == "loss_max_pct" else 600000 if key.endswith("ms") else 100000
                if not math.isfinite(value) or value < 0 or value > ceiling or (key == "deadline_ms" and value == 0):
                    raise ValueError(f"{name}: invalid {key}.")
            policy[name][key] = value
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
