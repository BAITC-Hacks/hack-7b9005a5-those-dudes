"""Shared role definitions used by scoring and the read-only explanation trace."""
from __future__ import annotations

import math

ROLES = ("consolidator", "transit", "distributor", "terminal", "coordinator", "peripheral")
PROFILE_TERMS = {
    "consolidator": ((.25, "p_in_deg"), (.20, "p_in_tx"), (.20, "p_in_kzt"), (.15, "p_authority"), (.20, "retention")),
    "transit": ((.30, "fifo_1d"), (.20, "balance_similarity"), (.20, "p_brokerage"), (.15, "p_total_tx"), (.15, "p_repeat_route")),
    "distributor": ((.25, "p_out_deg"), (.20, "p_out_tx"), (.20, "p_out_kzt"), (.15, "p_hub"), (.20, "sync_out_present")),
    "terminal": ((.35, "retention"), (.25, "p_in_kzt"), (.15, "p_in_tx"), (.15, "p_authority"), (.10, "no_continuation")),
    "coordinator": ((.25, "p_brokerage"), (.20, "p_seed_affinity"), (.20, "p_participation"), (.15, "pagerank_mean"), (.20, "motif")),
}


def add_role_profiles(frame):
    signals = {key: frame[key] for terms in PROFILE_TERMS.values() for _, key in terms if key in frame}
    for key in ("retention", "balance_similarity", "fifo_1d"):
        signals[key] = frame[key].fillna(0).clip(0, 1)
    signals["sync_out_present"] = frame["max_out_targets_day"].gt(0).astype(float) * frame["p_sync_out"]
    signals["no_continuation"] = 1 - frame["p_continue"]
    signals["pagerank_mean"] = frame[["p_pagerank_amount", "p_pagerank_count"]].mean(axis=1)
    signals["motif"] = frame[["p_sync_in", "p_sync_out", "p_repeat_route", "p_reciprocal", "p_cycle"]].mean(axis=1)
    for role, terms in PROFILE_TERMS.items():
        frame["score_" + role] = sum(weight * signals[name] for weight, name in terms)
    frame["score_peripheral"] = (1 - frame[["score_" + role for role in PROFILE_TERMS]].max(axis=1)).clip(.05, 1)


def profile_formula(role):
    return " + ".join(f"{weight:.2f}*{name}" for weight, name in PROFILE_TERMS[role])

def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None

def decision_trace(row: dict, metadata: dict) -> dict:
    """Authoritative eligibility conditions shared by scoring and explanations."""
    t = metadata.get("thresholds", {})
    degree = metadata.get("integer_gates", {})
    required = ("is_seed", "depth", "in_deg", "out_deg", "in_kzt", "in_flow_share", "out_flow_share",
                "fifo_1d", "balance_similarity", "truncated_by_depth", "p_continue",
                "p_brokerage", "p_seed_affinity", "p_participation", "p_pagerank_amount",
                "p_pagerank_count", "p_sync_in", "p_sync_out", "p_repeat_route", "p_reciprocal", "p_cycle")
    thresholds = ("flow_share_gate", "fifo_transit", "coordinator_rank_gate", "coordinator_score_gate",
                  "terminal_continue_allow", "in_kzt_median_positive_nonseed")
    if not all(_number(row.get(key)) is not None for key in required) or not all(key in t for key in thresholds) or not all(key in degree for key in ("in_degree", "out_degree")):
        return {"available": False, "reason": "Полные признаки или пороги не сохранены."}
    n = lambda key: float(row[key])
    check = lambda name, observed, op, threshold, passed: {"condition": name, "observed": observed, "operator": op, "threshold": threshold, "passed": bool(passed)}
    nonseed = check("is_seed", bool(row["is_seed"]), "=", False, not row["is_seed"])
    motif = sum(n(key) for key in ("p_sync_in", "p_sync_out", "p_repeat_route", "p_reciprocal", "p_cycle")) / 5
    signals = [n("p_brokerage"), n("p_seed_affinity"), n("p_participation"), (n("p_pagerank_amount") + n("p_pagerank_count")) / 2, motif]
    high_signals = sum(value >= t["coordinator_rank_gate"] for value in signals)
    terminal_observed = n("depth") < 4 or (bool(row["truncated_by_depth"]) and n("p_continue") <= t["terminal_continue_allow"])
    gates = {
        "consolidator": [nonseed, check("in_deg", n("in_deg"), ">=", degree["in_degree"], n("in_deg") >= degree["in_degree"]), check("in_flow_share", n("in_flow_share"), ">=", t["flow_share_gate"], n("in_flow_share") >= t["flow_share_gate"])],
        "transit": [nonseed, check("in_deg", n("in_deg"), ">", 0, n("in_deg") > 0), check("out_deg", n("out_deg"), ">", 0, n("out_deg") > 0), check("max(fifo_1d,balance_similarity)", max(n("fifo_1d"), n("balance_similarity")), ">=", t["fifo_transit"], max(n("fifo_1d"), n("balance_similarity")) >= t["fifo_transit"])],
        "distributor": [check("out_deg", n("out_deg"), ">=", degree["out_degree"], n("out_deg") >= degree["out_degree"]), check("out_flow_share", n("out_flow_share"), ">=", t["flow_share_gate"], n("out_flow_share") >= t["flow_share_gate"])],
        "terminal": [nonseed, check("in_deg", n("in_deg"), ">", 0, n("in_deg") > 0), check("out_deg", n("out_deg"), "=", 0, n("out_deg") == 0), check("depth<4 OR (censored AND p_continue<=threshold)", {"depth": n("depth"), "p_continue": n("p_continue"), "censored": bool(row["truncated_by_depth"])}, "rule", t["terminal_continue_allow"], terminal_observed), check("in_kzt", n("in_kzt"), ">=", t["in_kzt_median_positive_nonseed"], n("in_kzt") >= t["in_kzt_median_positive_nonseed"])],
        "coordinator": [check("signals >= rank gate", high_signals, ">=", 3, high_signals >= 3), check("max(p_brokerage,p_participation)", max(n("p_brokerage"), n("p_participation")), ">=", .9, max(n("p_brokerage"), n("p_participation")) >= .9), check("score_coordinator", _number(row.get("score_coordinator")), ">=", t["coordinator_score_gate"], (_number(row.get("score_coordinator")) or 0) >= t["coordinator_score_gate"])],
        "peripheral": [],
    }
    return {"available": True, "selection_rule": "maximum profile_score among eligible roles; ties follow saved role order", "role_order": list(ROLES),
            "roles": {role: {"eligible": all(item["passed"] for item in checks), "profile_score": _number(row.get(f"score_{role}")), "conditions": checks} for role, checks in gates.items()},
            "coordinator_rank_gate": t["coordinator_rank_gate"], "coordinator_signals": signals,
            "role_score_formula": metadata.get("role_score_formula")}
