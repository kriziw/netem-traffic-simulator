"""One executable workload manifest shared by generation and scoring."""
import math
from .profiles import APPLICATIONS, PERSONAS, ACTIVITY


def allocate_counts(weights, users):
    positive = {key: float(value) for key, value in weights.items() if value > 0}
    total = sum(positive.values())
    quotas = {key: users * value / total for key, value in positive.items()}
    counts = {key: int(value) for key, value in quotas.items()}
    for key in sorted(quotas, key=lambda key: (-(quotas[key] - counts[key]), key))[:users - sum(counts.values())]:
        counts[key] += 1
    return {key: count for key, count in counts.items() if count}


def application_weights(persona, global_mix):
    base = PERSONAS.get(persona, PERSONAS["knowledge_worker"])["applications"]
    weights = {app: float(base.get(app, 0)) * float(global_mix.get(app, 0)) for app in APPLICATIONS}
    return weights if sum(weights.values()) else dict(global_mix)


def balanced_application(config, persona, weights, rng):
    """Smooth weighted selection shared by a persona's users, without extra work.

    Selection runs without yielding on the traffic greenlet. Deficits reset when
    the mix changes; random tie-breaking avoids fixed application startup order.
    Shares refer to user actions, just as they did for random selection.
    """
    positive = {key: float(value) for key, value in weights.items() if value > 0}
    total = sum(positive.values())
    if not total:
        raise ValueError("Application selection requires a positive workload share.")
    signature = sorted(positive.items())
    states = config.setdefault("application_selection", {})
    state = states.get(persona)
    if state is None or state["weights"] != signature:
        state = states[persona] = {"weights": signature, "deficits": {key: 0.0 for key in positive}}
    deficits = state["deficits"]
    for key, weight in positive.items():
        deficits[key] += weight / total
    highest = max(deficits.values())
    chosen = rng.choice([key for key in positive if abs(deficits[key] - highest) < 1e-12])
    deficits[chosen] -= 1
    return chosen


def compile_manifest(config, users=None):
    counts = allocate_counts(config["personas"], users or config["target_users"])
    shares, per_persona = {}, {}
    for persona, count in counts.items():
        weights = application_weights(persona, config["applications"])
        total = sum(weights.values())
        per_persona[persona] = {app: weight / total for app, weight in weights.items() if weight}
        for app, weight in weights.items():
            if weight:
                shares[app] = shares.get(app, 0) + count / sum(counts.values()) * weight / total
    if config.get("paced_mbps"):
        shares = {"guest_internet": 1.0}
        if config.get("sparse_slots"):
            shares["ot_telemetry"] = config["sparse_slots"] / (users or config["target_users"])
            shares["guest_internet"] -= shares["ot_telemetry"]
    guidance = {}
    if not config.get("paced_mbps"):
        bounds = ACTIVITY.get(config.get("activity", "normal"), ACTIVITY["normal"])
        mean_wait = (bounds["wait_min"] + bounds["wait_max"]) / 2
        for app, share in shares.items():
            upper_rate = sum(counts.values()) * share / mean_wait
            guidance[app] = {"optimistic_transactions_per_60s": round(upper_rate * 60, 3),
                             "minimum_mean_window_s": math.ceil(5 / upper_rate),
                             "sparse_in_60s": upper_rate * 60 < 5,
                             "optimistic_transactions_per_live_window": round(upper_rate * config.get("live_window_seconds", 15), 3),
                             "sparse_in_live_window": upper_rate * config.get("live_window_seconds", 15) < 5,
                             "selection": "balanced weighted user actions",
                             "basis": "zero service-time user model; real operation duration lowers arrivals; not a guarantee"}
    return {"sampling_guidance": guidance, "persona_counts": counts, "application_shares": shares, "persona_applications": per_persona,
            "share_basis": "configured user activity", "version": 1}
