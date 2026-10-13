"""One executable workload manifest shared by generation and scoring."""
from .profiles import APPLICATIONS, PERSONAS


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
    return {"persona_counts": counts, "application_shares": shares, "persona_applications": per_persona,
            "share_basis": "configured user activity", "version": 1}
