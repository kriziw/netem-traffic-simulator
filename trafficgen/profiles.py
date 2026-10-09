from __future__ import annotations

import math

APPLICATIONS = {
    "web_saas": {
        "label": "Web / SaaS",
        "description": "Interactive HTTPS page/API transactions and small objects.",
        "latency_good_ms": 300,
        "latency_poor_ms": 1200,
    },
    "collaboration": {
        "label": "Collaboration",
        "description": "Chat/presence polling and small bidirectional transactions.",
        "latency_good_ms": 250,
        "latency_poor_ms": 900,
    },
    "voice": {
        "label": "Voice",
        "description": "Low-bandwidth UDP conversational media bursts.",
        "latency_good_ms": 180,
        "latency_poor_ms": 500,
    },
    "video": {
        "label": "Video meeting",
        "description": "Variable-rate UDP video/media bursts.",
        "latency_good_ms": 250,
        "latency_poor_ms": 800,
    },
    "file_sync": {
        "label": "Cloud file sync",
        "description": "Bursty uploads and downloads of medium-sized objects.",
        "latency_good_ms": 800,
        "latency_poor_ms": 3000,
    },
    "developer": {
        "label": "Developer / packages",
        "description": "API, package and artifact-style HTTPS transfers.",
        "latency_good_ms": 700,
        "latency_poor_ms": 2500,
    },
    "updates": {
        "label": "Software updates",
        "description": "Intermittent large downloads.",
        "latency_good_ms": 1200,
        "latency_poor_ms": 5000,
    },
    "backup": {
        "label": "Backup",
        "description": "Sustained upload-oriented background transfers.",
        "latency_good_ms": 1500,
        "latency_poor_ms": 6000,
    },
    "dns": {
        "label": "DNS",
        "description": "Small DNS-like application transactions through the target service.",
        "latency_good_ms": 120,
        "latency_poor_ms": 600,
    },
}

PERSONAS = {
    "knowledge_worker": {
        "label": "Knowledge worker",
        "applications": {
            "web_saas": 42,
            "collaboration": 20,
            "file_sync": 14,
            "dns": 10,
            "video": 8,
            "updates": 4,
            "voice": 2,
        },
    },
    "collaboration_user": {
        "label": "Collaboration user",
        "applications": {
            "collaboration": 28,
            "video": 27,
            "voice": 22,
            "web_saas": 13,
            "dns": 5,
            "file_sync": 5,
        },
    },
    "developer": {
        "label": "Developer",
        "applications": {
            "developer": 36,
            "web_saas": 22,
            "file_sync": 18,
            "dns": 8,
            "collaboration": 8,
            "updates": 8,
        },
    },
    "heavy_cloud": {
        "label": "Heavy cloud user",
        "applications": {
            "file_sync": 32,
            "web_saas": 24,
            "video": 16,
            "developer": 10,
            "collaboration": 8,
            "updates": 6,
            "dns": 4,
        },
    },
    "background": {
        "label": "Background / services",
        "applications": {
            "updates": 28,
            "backup": 28,
            "dns": 18,
            "web_saas": 14,
            "file_sync": 12,
        },
    },
}

ACTIVITY = {
    "light": {"wait_min": 8.0, "wait_max": 30.0},
    "normal": {"wait_min": 3.0, "wait_max": 14.0},
    "busy": {"wait_min": 1.0, "wait_max": 6.0},
    "peak": {"wait_min": 0.4, "wait_max": 2.5},
}

PATTERNS = {
    "steady": {
        "label": "Steady",
        "description": "Ramp to the target population and hold until stopped.",
        "stages": [],
    },
    "office_day": {
        "label": "Office day · 8h",
        "description": "Arrival, collaboration peaks, lunch dip and end-of-day decline.",
        "stages": [
            {"after": 0, "users_factor": 0.15, "activity": "light"},
            {"after": 1800, "users_factor": 0.65, "activity": "normal"},
            {"after": 1800, "users_factor": 1.0, "activity": "normal"},
            {"after": 3600, "users_factor": 1.0, "activity": "busy"},
            {"after": 7200, "users_factor": 0.55, "activity": "light"},
            {"after": 3600, "users_factor": 1.0, "activity": "normal"},
            {"after": 5400, "users_factor": 1.0, "activity": "busy"},
            {"after": 3600, "users_factor": 0.45, "activity": "light"},
        ],
    },
    "office_day_compressed": {
        "label": "Office day · compressed 20m",
        "description": "The office-day population/activity curve compressed for lab validation.",
        "stages": [
            {"after": 0, "users_factor": 0.20, "activity": "light"},
            {"after": 120, "users_factor": 0.70, "activity": "normal"},
            {"after": 180, "users_factor": 1.0, "activity": "busy"},
            {"after": 300, "users_factor": 0.55, "activity": "light"},
            {"after": 180, "users_factor": 1.0, "activity": "peak"},
            {"after": 300, "users_factor": 0.35, "activity": "light"},
        ],
    },
    "meeting_peak": {
        "label": "Meeting peak",
        "description": "Rapid ramp to a busy collaboration-heavy period.",
        "stages": [
            {"after": 0, "users_factor": 0.5, "activity": "normal"},
            {"after": 60, "users_factor": 1.0, "activity": "peak"},
        ],
    },
}

WORKLOAD_PROFILES = {
    "office": {
        "label": "Office workload",
        "description": "Balanced corporate branch with knowledge workers and collaboration.",
        "activity": "normal",
        "pattern": "steady",
        "personas": {
            "knowledge_worker": 55,
            "collaboration_user": 20,
            "developer": 10,
            "heavy_cloud": 10,
            "background": 5,
        },
        "applications": {
            "web_saas": 27,
            "collaboration": 17,
            "video": 13,
            "voice": 8,
            "file_sync": 12,
            "developer": 7,
            "updates": 5,
            "backup": 3,
            "dns": 8,
        },
    },
    "video_heavy": {
        "label": "Video-heavy office",
        "description": "Collaboration and conferencing dominate user experience.",
        "activity": "busy",
        "pattern": "meeting_peak",
        "personas": {
            "knowledge_worker": 30,
            "collaboration_user": 50,
            "developer": 5,
            "heavy_cloud": 10,
            "background": 5,
        },
        "applications": {
            "web_saas": 12,
            "collaboration": 20,
            "video": 32,
            "voice": 18,
            "file_sync": 6,
            "developer": 2,
            "updates": 2,
            "backup": 1,
            "dns": 7,
        },
    },
    "developer_office": {
        "label": "Developer office",
        "description": "Package, artifact, Git-like and cloud-development traffic.",
        "activity": "busy",
        "pattern": "steady",
        "personas": {
            "knowledge_worker": 15,
            "collaboration_user": 10,
            "developer": 55,
            "heavy_cloud": 15,
            "background": 5,
        },
        "applications": {
            "web_saas": 16,
            "collaboration": 8,
            "video": 6,
            "voice": 3,
            "file_sync": 17,
            "developer": 29,
            "updates": 9,
            "backup": 4,
            "dns": 8,
        },
    },
    "cloud_heavy": {
        "label": "Cloud-heavy branch",
        "description": "File synchronization, SaaS and large object transfer dominate.",
        "activity": "busy",
        "pattern": "steady",
        "personas": {
            "knowledge_worker": 30,
            "collaboration_user": 10,
            "developer": 10,
            "heavy_cloud": 45,
            "background": 5,
        },
        "applications": {
            "web_saas": 20,
            "collaboration": 8,
            "video": 10,
            "voice": 3,
            "file_sync": 27,
            "developer": 9,
            "updates": 8,
            "backup": 8,
            "dns": 7,
        },
    },
    "backup_window": {
        "label": "Backup window",
        "description": "Background backup and software distribution with a small user population.",
        "activity": "normal",
        "pattern": "steady",
        "personas": {
            "knowledge_worker": 10,
            "collaboration_user": 5,
            "developer": 5,
            "heavy_cloud": 10,
            "background": 70,
        },
        "applications": {
            "web_saas": 5,
            "collaboration": 2,
            "video": 1,
            "voice": 1,
            "file_sync": 14,
            "developer": 5,
            "updates": 27,
            "backup": 39,
            "dns": 6,
        },
    },
}


def normalized_mix(values: dict, allowed: dict) -> dict:
    if not isinstance(values, dict) or not values:
        raise ValueError("Mix must be a nonempty object.")
    if set(values) - set(allowed):
        raise ValueError("Mix contains unknown entries.")
    clean = {}
    for key in allowed:
        try:
            value = float(values.get(key, 0))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Mix weights must be finite nonnegative numbers.") from None
        if not math.isfinite(value) or value < 0:
            raise ValueError("Mix weights must be finite nonnegative numbers.")
        clean[key] = value
    total = sum(clean.values())
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Mix must have at least one positive weight.")
    return {key: value / total * 100.0 for key, value in clean.items()}


def profile_payload():
    return {
        "profiles": WORKLOAD_PROFILES,
        "personas": PERSONAS,
        "applications": APPLICATIONS,
        "activity_levels": ACTIVITY,
        "patterns": PATTERNS,
    }
