from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


ROLES = (
    "consolidator",
    "transit",
    "distributor",
    "terminal",
    "coordinator",
    "peripheral",
)


def signal_percentile(series: pd.Series) -> pd.Series:
    """ECDF rank where an absent/non-positive signal stays exactly zero."""

    values = pd.to_numeric(series, errors="coerce").fillna(0.0).clip(lower=0.0)
    result = pd.Series(0.0, index=series.index, dtype=float)
    positive = values > 0
    if positive.any():
        result.loc[positive] = values.loc[positive].rank(method="average", pct=True)
    return result


def _sigmoid(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-values))


@dataclass(frozen=True)
class ContinuationResult:
    probability: pd.Series
    metadata: dict[str, object]


def fit_continuation_model(features: pd.DataFrame) -> ContinuationResult:
    """Transparent L2 logistic model trained on observed depth 1–3 nodes.

    The target is whether an outgoing edge is observed, not a role label. Only
    inbound-side features are used so the model can be applied to censored
    depth-4 nodes without peeking at unavailable outflow.
    """

    columns = [
        "in_deg",
        "in_tx",
        "in_kzt",
        "active_in_days",
        "in_source_hhi",
        "direct_seed_in",
        "seed_affinity",
        "first_in_day",
        "last_in_day",
    ]
    transformed = pd.DataFrame(index=features.index)
    for column in columns:
        values = pd.to_numeric(features[column], errors="coerce").fillna(0.0).astype(float)
        transformed[column] = np.log1p(values) if column in {"in_deg", "in_tx", "in_kzt", "active_in_days", "direct_seed_in", "seed_affinity"} else values

    train_mask = (
        (features["depth"].between(1, 3))
        & (~features["is_seed"].astype(bool))
        & (features["in_deg"] > 0)
    )
    training = transformed.loc[train_mask]
    target = (features.loc[train_mask, "out_deg"] > 0).astype(float).to_numpy()
    if len(training) < 20 or len(np.unique(target)) < 2:
        baseline = float(target.mean()) if len(target) else 0.5
        probability = pd.Series(baseline, index=features.index, dtype=float)
        return ContinuationResult(
            probability=probability,
            metadata={
                "model": "constant_fallback",
                "baseline": baseline,
                "n_train": int(len(training)),
                "warning": "Недостаточно данных или классов для logistic-модели",
            },
        )

    mean = training.mean(axis=0)
    scale = training.std(axis=0, ddof=0).replace(0.0, 1.0)
    x_train = (training - mean) / scale
    design = np.column_stack([np.ones(len(x_train)), x_train.to_numpy(dtype=float)])
    beta = np.zeros(design.shape[1], dtype=float)
    regularization = 1.0
    iterations = 0
    for iteration in range(100):
        probability = _sigmoid(design @ beta)
        weights = np.clip(probability * (1.0 - probability), 1e-6, None)
        hessian = design.T @ (weights[:, None] * design)
        penalty = np.eye(len(beta)) * regularization
        penalty[0, 0] = 0.0
        gradient = design.T @ (target - probability) - penalty @ beta
        try:
            step = np.linalg.solve(hessian + penalty + np.eye(len(beta)) * 1e-8, gradient)
        except np.linalg.LinAlgError:
            step = np.linalg.pinv(hessian + penalty + np.eye(len(beta)) * 1e-8) @ gradient
        beta += step
        iterations = iteration + 1
        if float(np.max(np.abs(step))) < 1e-8:
            break

    all_standardized = (transformed - mean) / scale
    all_design = np.column_stack([np.ones(len(features)), all_standardized.to_numpy(dtype=float)])
    all_probability = _sigmoid(all_design @ beta)
    train_probability = _sigmoid(design @ beta)
    brier = float(np.mean(np.square(train_probability - target)))
    positive_ranks = pd.Series(train_probability).rank(method="average").to_numpy()
    n_positive = int(target.sum())
    n_negative = int(len(target) - n_positive)
    auc = (
        float((positive_ranks[target == 1].sum() - n_positive * (n_positive + 1) / 2) / (n_positive * n_negative))
        if n_positive and n_negative
        else float("nan")
    )
    coefficient_map = {"intercept": float(beta[0])}
    coefficient_map.update({column: float(value) for column, value in zip(columns, beta[1:])})
    metadata: dict[str, object] = {
        "model": "l2_logistic_irls_standardized",
        "target": "observed_out_degree_gt_0_on_depth_1_3",
        "n_train": int(len(training)),
        "positive_rate": float(target.mean()),
        "iterations": int(iterations),
        "regularization": regularization,
        "train_brier": brier,
        "train_auc_descriptive_only": auc,
        "features": columns,
        "means": {column: float(mean[column]) for column in columns},
        "scales": {column: float(scale[column]) for column in columns},
        "standardized_coefficients": coefficient_map,
        "limitation": "Оценивает продолжение наблюдаемого маршрута, а не истинную роль; depth=4 остаётся правоцензурирован",
    }
    return ContinuationResult(
        probability=pd.Series(all_probability, index=features.index, dtype=float),
        metadata=metadata,
    )


def _positive_quantile(series: pd.Series, quantile: float, fallback: float) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    values = values[values > 0]
    return float(values.quantile(quantile)) if len(values) else float(fallback)


def _round_gate(value: float, minimum: int = 1) -> int:
    return max(minimum, int(np.ceil(value - 1e-12)))


def role_and_priority_scores(
    features: pd.DataFrame,
    random_seed: int = 42,
    stability_runs: int = 40,
) -> tuple[pd.DataFrame, dict[str, object]]:
    frame = features.copy()
    continuation = fit_continuation_model(frame)
    frame["p_continue"] = continuation.probability.clip(0.0, 1.0)

    rank_sources = {
        "p_in_deg": "in_deg",
        "p_out_deg": "out_deg",
        "p_in_tx": "in_tx",
        "p_out_tx": "out_tx",
        "p_in_kzt": "in_kzt",
        "p_out_kzt": "out_kzt",
        "p_total_kzt": "total_kzt",
        "p_total_tx": "total_tx",
        "p_pagerank_amount": "pagerank_amount",
        "p_pagerank_count": "pagerank_count",
        "p_authority": "hits_authority",
        "p_hub": "hits_hub",
        "p_betweenness": "betweenness",
        "p_weighted_betweenness": "weighted_betweenness",
        "p_brokerage": "brokerage",
        "p_seed_affinity": "seed_affinity",
        "p_direct_seed": "direct_seed_in",
        "p_participation": "participation",
        "p_fragmentation": "fragmentation_impact",
        "p_sync_in": "max_in_sources_day",
        "p_sync_out": "max_out_targets_day",
        "p_repeat_route": "repeat_route_count",
        "p_repeat_edge": "repeated_in_edges",
        "p_reciprocal": "reciprocal_neighbors",
        "p_cycle": "temporal_cycle_count",
        "p_boundary_kzt": "boundary_kzt",
        "p_cluster_touch": "clusters_touched",
    }
    for rank_column, source_column in rank_sources.items():
        frame[rank_column] = signal_percentile(frame[source_column])

    nonseed = ~frame["is_seed"].astype(bool)
    thresholds = {
        "in_degree_q95_positive_nonseed": _positive_quantile(frame.loc[nonseed, "in_deg"], 0.95, 3),
        "out_degree_q90_positive_nonseed": _positive_quantile(frame.loc[nonseed, "out_deg"], 0.90, 8),
        "in_kzt_median_positive_nonseed": _positive_quantile(frame.loc[nonseed, "in_kzt"], 0.50, 50_000),
        "fifo_transit": 0.80,
        "flow_share_gate": 0.80,
        "coordinator_rank_gate": 0.90,
        "coordinator_score_gate": 0.75,
        "terminal_continue_allow": 0.20,
        "terminal_continue_reject": 0.50,
        "ambiguity_margin": 0.10,
    }
    in_degree_gate = _round_gate(thresholds["in_degree_q95_positive_nonseed"])
    out_degree_gate = _round_gate(thresholds["out_degree_q90_positive_nonseed"])
    inbound_strength = (frame["p_in_deg"] + frame["p_in_tx"] + frame["p_in_kzt"]) / 3.0
    outbound_strength = (frame["p_out_deg"] + frame["p_out_tx"] + frame["p_out_kzt"]) / 3.0
    influence = (
        frame[["p_pagerank_amount", "p_pagerank_count", "p_authority", "p_hub"]].max(axis=1)
    )
    motif = (
        frame[["p_sync_in", "p_sync_out", "p_repeat_route", "p_reciprocal", "p_cycle"]]
        .mean(axis=1)
    )

    frame["score_consolidator"] = (
        0.25 * frame["p_in_deg"]
        + 0.20 * frame["p_in_tx"]
        + 0.20 * frame["p_in_kzt"]
        + 0.15 * frame["p_authority"]
        + 0.20 * frame["retention"].clip(0.0, 1.0)
    )
    frame["score_transit"] = (
        0.30 * frame["fifo_1d"].fillna(0.0).clip(0.0, 1.0)
        + 0.20 * frame["balance_similarity"].clip(0.0, 1.0)
        + 0.20 * frame["p_brokerage"]
        + 0.15 * frame["p_total_tx"]
        + 0.15 * frame["p_repeat_route"]
    )
    frame["score_distributor"] = (
        0.25 * frame["p_out_deg"]
        + 0.20 * frame["p_out_tx"]
        + 0.20 * frame["p_out_kzt"]
        + 0.15 * frame["p_hub"]
        + 0.20 * frame["max_out_targets_day"].gt(0).astype(float) * frame["p_sync_out"]
    )
    frame["score_terminal"] = (
        0.35 * frame["retention"].clip(0.0, 1.0)
        + 0.25 * frame["p_in_kzt"]
        + 0.15 * frame["p_in_tx"]
        + 0.15 * frame["p_authority"]
        + 0.10 * (1.0 - frame["p_continue"])
    )
    frame["score_coordinator"] = (
        0.25 * frame["p_brokerage"]
        + 0.20 * frame["p_seed_affinity"]
        + 0.20 * frame["p_participation"]
        + 0.15 * frame[["p_pagerank_amount", "p_pagerank_count"]].mean(axis=1)
        + 0.20 * motif
    )
    nonperipheral_columns = [f"score_{role}" for role in ROLES if role != "peripheral"]
    frame["score_peripheral"] = (1.0 - frame[nonperipheral_columns].max(axis=1)).clip(0.05, 1.0)

    coordinator_signals = pd.concat(
        [
            frame["p_brokerage"],
            frame["p_seed_affinity"],
            frame["p_participation"],
            frame[["p_pagerank_amount", "p_pagerank_count"]].mean(axis=1),
            motif,
        ],
        axis=1,
    )
    sinks = (frame["in_deg"] > 0) & (frame["out_deg"] == 0) & nonseed
    lower_terminal = sinks & (frame["depth"] < 4)
    censored_terminal = (
        sinks
        & frame["truncated_by_depth"].astype(bool)
        & (frame["p_continue"] <= thresholds["terminal_continue_allow"])
    )
    gates = pd.DataFrame(
        {
            "consolidator": nonseed
            & (frame["in_deg"] >= in_degree_gate)
            & (frame["in_flow_share"] >= thresholds["flow_share_gate"]),
            "transit": nonseed
            & (frame["in_deg"] > 0)
            & (frame["out_deg"] > 0)
            & (
                (frame["fifo_1d"] >= thresholds["fifo_transit"])
                | (frame["balance_similarity"] >= thresholds["fifo_transit"])
            ),
            "distributor": (frame["out_deg"] >= out_degree_gate)
            & (frame["out_flow_share"] >= thresholds["flow_share_gate"]),
            "terminal": (lower_terminal | censored_terminal)
            & (frame["in_kzt"] >= thresholds["in_kzt_median_positive_nonseed"]),
            "coordinator": (coordinator_signals.ge(thresholds["coordinator_rank_gate"]).sum(axis=1) >= 3)
            & ((frame["p_brokerage"] >= 0.90) | (frame["p_participation"] >= 0.90))
            & (frame["score_coordinator"] >= thresholds["coordinator_score_gate"]),
            "peripheral": True,
        },
        index=frame.index,
    )

    score_columns = [f"score_{role}" for role in ROLES]
    raw_scores = frame[score_columns].to_numpy(dtype=float)
    eligible_scores = raw_scores.copy()
    for index, role in enumerate(ROLES):
        eligible_scores[~gates[role].to_numpy(dtype=bool), index] = -np.inf
    primary_index = np.argmax(eligible_scores, axis=1)
    roles = np.asarray(ROLES, dtype=object)
    frame["role"] = roles[primary_index]
    top_score = eligible_scores[np.arange(len(frame)), primary_index]
    second_scores = eligible_scores.copy()
    second_scores[np.arange(len(frame)), primary_index] = -np.inf
    secondary_index = np.argmax(second_scores, axis=1)
    second_score = second_scores[np.arange(len(frame)), secondary_index]
    no_second = ~np.isfinite(second_score)
    if no_second.any():
        fallback = raw_scores[:, :5].copy()
        primary_is_nonperipheral = primary_index < 5
        fallback[
            np.arange(len(frame))[primary_is_nonperipheral],
            primary_index[primary_is_nonperipheral],
        ] = -np.inf
        secondary_index[no_second] = np.argmax(fallback[no_second], axis=1)
        second_score[no_second] = np.max(fallback[no_second], axis=1)
    frame["secondary_role"] = roles[secondary_index]
    margin = np.maximum(0.0, top_score - second_score)

    rng = np.random.default_rng(random_seed)
    same_role = np.zeros(len(frame), dtype=float)
    for _ in range(stability_runs):
        perturbed = np.clip(raw_scores + rng.normal(0.0, 0.035, raw_scores.shape), 0.0, 1.0)
        for index, role in enumerate(ROLES):
            perturbed[~gates[role].to_numpy(dtype=bool), index] = -np.inf
        same_role += (np.argmax(perturbed, axis=1) == primary_index).astype(float)
    frame["role_stability"] = same_role / max(1, stability_runs)

    observability = np.ones(len(frame), dtype=float)
    observability[(frame["is_seed"].astype(bool)) & (frame["role"] == "coordinator")] = 0.85
    observability[frame["truncated_by_depth"].astype(bool)] = 0.70
    observability[(frame["role"] == "terminal") & frame["truncated_by_depth"].astype(bool)] = 0.55
    isolated = (frame["in_deg"] + frame["out_deg"]) == 0
    observability[isolated.to_numpy()] = 0.45
    frame["observability"] = observability
    frame["role_score"] = np.clip(
        observability
        * (
            0.45 * np.clip(top_score, 0.0, 1.0)
            + 0.30 * np.minimum(1.0, margin / 0.25)
            + 0.25 * frame["role_stability"].to_numpy()
        ),
        0.05,
        0.99,
    )
    terminal_censored_mask = (frame["role"] == "terminal") & frame["truncated_by_depth"].astype(bool)
    frame.loc[terminal_censored_mask, "role_score"] = frame.loc[terminal_censored_mask, "role_score"].clip(upper=0.60)
    frame.loc[isolated & frame["is_seed"].astype(bool), "role_score"] = frame.loc[
        isolated & frame["is_seed"].astype(bool), "role_score"
    ].clip(upper=0.35)

    uncertainty: list[str] = []
    for row_index, row in frame.iterrows():
        reasons: list[str] = []
        if bool(row["truncated_by_depth"]):
            reasons.append(f"depth=4 censored,p_continue={row['p_continue']:.2f}")
        if margin[row_index] < thresholds["ambiguity_margin"]:
            reasons.append(f"role margin={margin[row_index]:.2f}")
        if float(row["cluster_stability"]) < 0.70:
            reasons.append(f"cluster stability={row['cluster_stability']:.2f}")
        if int(row["in_deg"] + row["out_deg"]) == 0:
            reasons.append("нет наблюдаемых рёбер")
        if bool(row["is_seed"]) and row["role"] in {"consolidator", "transit", "terminal"}:
            reasons.append("вход seed неполон")
        uncertainty.append("; ".join(reasons) if reasons else "нет существенной по наблюдаемым данным")
    frame["uncertainty_reason"] = uncertainty

    connectivity = (
        0.40 * frame["p_brokerage"]
        + 0.30 * frame["p_fragmentation"]
        + 0.30 * frame["p_participation"]
    )
    exposure = (
        0.35 * frame["p_total_kzt"]
        + 0.25 * frame["p_total_tx"]
        + 0.40 * influence
    )
    seed_component = (
        0.55 * frame["p_seed_affinity"]
        + 0.30 * frame["p_direct_seed"]
        + 0.15 * signal_percentile(frame["n_seed_reachable"])
    )
    matched_flow = pd.Series(
        np.minimum(frame["in_kzt"].to_numpy(), frame["out_kzt"].to_numpy()),
        index=frame.index,
    )
    fifo_exposure = frame["fifo_1d"].fillna(0.0).clip(0.0, 1.0) * signal_percentile(
        matched_flow
    )
    temporal = (
        0.35 * fifo_exposure
        + 0.25 * frame[["p_sync_in", "p_sync_out"]].max(axis=1)
        + 0.20 * frame[["p_repeat_route", "p_repeat_edge"]].max(axis=1)
        + 0.20 * frame[["p_reciprocal", "p_cycle"]].max(axis=1)
    )
    role_expression = frame[nonperipheral_columns].max(axis=1)
    uncertainty_value = (1.0 - frame["role_score"]) * exposure
    frame["priority_connectivity"] = connectivity
    frame["priority_exposure"] = exposure
    frame["priority_seed"] = seed_component
    frame["priority_temporal"] = temporal
    frame["priority_role"] = role_expression
    frame["priority_uncertainty"] = uncertainty_value
    frame["priority_raw"] = (
        0.30 * connectivity
        + 0.22 * exposure
        + 0.18 * seed_component
        + 0.15 * temporal
        + 0.10 * role_expression
        + 0.05 * uncertainty_value
    )
    frame["priority_score"] = frame["priority_raw"].rank(method="average", pct=True)

    frame["evidence"] = [_role_evidence(row) for _, row in frame.iterrows()]
    metadata: dict[str, object] = {
        "roles": list(ROLES),
        "thresholds": thresholds,
        "integer_gates": {"in_degree": in_degree_gate, "out_degree": out_degree_gate},
        "role_score_formula": "observability*(0.45*profile+0.30*min(1,margin/0.25)+0.25*score_perturbation_stability)",
        "priority_formula": "ECDF(0.30*K+0.22*E+0.18*S+0.15*T+0.10*X+0.05*U)",
        "score_perturbation_runs": int(stability_runs),
        "random_seed": int(random_seed),
        "continuation_model": continuation.metadata,
    }
    return frame, metadata


def _role_evidence(row: pd.Series) -> str:
    role = str(row["role"])
    if role == "consolidator":
        text = (
            f"вход={int(row.in_deg)} источн./{int(row.in_tx)} tx/{row.in_kzt:.0f} KZT; "
            f"удержание={row.retention:.2f}; authority p={row.p_authority:.2f}"
        )
    elif role == "transit":
        ratio = row.pass_through if pd.notna(row.pass_through) else 0.0
        text = (
            f"FIFO<=1д={row.fifo_1d:.0%}; out/in={ratio:.2f}; лаг={row.median_fifo_lag if pd.notna(row.median_fifo_lag) else -1:.0f}д; "
            f"broker p={row.p_brokerage:.2f}"
        )
    elif role == "distributor":
        text = (
            f"выход={int(row.out_deg)} получ./{int(row.out_tx)} tx/{row.out_kzt:.0f} KZT; "
            f"max/day={int(row.max_out_targets_day)}; hub p={row.p_hub:.2f}"
        )
    elif role == "terminal":
        if bool(row.truncated_by_depth):
            text = (
                f"depth=4 censored; p_continue={row.p_continue:.2f}; вход={row.in_kzt:.0f} KZT/{int(row.in_tx)} tx; "
                "confidence<=0.60"
            )
        else:
            text = (
                f"наблюдаемый sink depth={int(row.depth)}; вход={row.in_kzt:.0f} KZT/{int(row.in_tx)} tx; "
                f"удержание={row.retention:.2f}"
            )
    elif role == "coordinator":
        text = (
            f"seed direct={int(row.direct_seed_in)}, seed p={row.p_seed_affinity:.2f}; "
            f"broker p={row.p_brokerage:.2f}; clusters={int(row.clusters_touched)}; sync={max(int(row.max_in_sources_day), int(row.max_out_targets_day))}"
        )
    else:
        text = (
            f"слабый профиль: in/out deg={int(row.in_deg)}/{int(row.out_deg)}, оборот={row.total_kzt:.0f} KZT; "
            f"лучший={row.secondary_role}:{row.get('score_' + str(row.secondary_role), 0.0):.2f}"
        )
    return text[:200]
