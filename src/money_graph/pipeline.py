from __future__ import annotations

import json
import platform
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd

from . import clustering
from .features import assemble_base_features, build_graph
from .io import InputData, load_inputs
from .resilience import resilience_analysis
from .scoring import ROLES, role_and_priority_scores
from .validation import validate_outputs
from .contracts import CSV_COLUMNS
from .narration import ROLE_RU, amount, priority_why
from .rules import decision_trace


@dataclass(frozen=True)
class PipelineConfig:
    data_dir: Path
    output_dir: Path
    random_seed: int = 42
    cluster_runs: int = 16
    role_stability_runs: int = 40
    top_n: int = 50
    validate: bool = False


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, Path):
        return str(value)
    return value


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(_json_value(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _cluster_table(
    features: pd.DataFrame,
    edges: pd.DataFrame,
    ordered_communities: list[set[int]],
) -> pd.DataFrame:
    cluster_lookup = features.set_index("gid")["cluster_id"].astype(int).to_dict()
    internal: dict[int, float] = {cluster_id: 0.0 for cluster_id in range(len(ordered_communities))}
    external: dict[int, float] = {cluster_id: 0.0 for cluster_id in range(len(ordered_communities))}
    for row in edges.itertuples(index=False):
        source_cluster = cluster_lookup[int(row.src)]
        target_cluster = cluster_lookup[int(row.dst)]
        if source_cluster == target_cluster:
            internal[source_cluster] += float(row.sum_kzt)
        else:
            external[source_cluster] += float(row.sum_kzt)
            external[target_cluster] += float(row.sum_kzt)

    rows: list[dict[str, object]] = []
    for cluster_id, community in enumerate(ordered_communities):
        subset = features[features["cluster_id"] == cluster_id].copy()
        role_counts = subset["role"].value_counts()
        nonperipheral = role_counts.drop(labels=["peripheral"], errors="ignore")
        if len(nonperipheral):
            dominant_role = str(nonperipheral.index[0])
            dominant_count = int(nonperipheral.iloc[0])
        else:
            dominant_role = "peripheral"
            dominant_count = int(role_counts.get("peripheral", 0))
        top = subset.sort_values(["priority_score", "gid"], ascending=[False, True]).head(5)
        fifo_count = int((subset["fifo_1d"] >= 0.80).sum())
        hypothesis = (
            f"Гипотеза: {ROLE_RU[dominant_role]} у {dominant_count} из {len(subset)} узлов. "
            f"Исходных узлов обхода: {int(subset['is_seed'].sum())}; внутренние переводы {amount(internal[cluster_id])}. "
            f"У {fifo_count} узлов сопоставлено не менее 80% меньшего из входящего/исходящего объёмов в тот же или следующий день. "
            "Выборка неполная; проверить маршруты, не делать вывод о виновности."
        )
        rows.append(
            {
                "cluster_id": int(cluster_id),
                "n_nodes": int(len(subset)),
                "n_seed": int(subset["is_seed"].sum()),
                "sum_kzt_internal": float(internal[cluster_id]),
                "top_gids": "|".join(top["gid"].astype(str)),
                "hypothesis": hypothesis,
                "sum_kzt_boundary": float(external[cluster_id]),
                "dominant_role": dominant_role,
                "dominant_role_count": dominant_count,
                "mean_cluster_stability": float(subset["cluster_stability"].mean()),
                "n_fifo_1d_80": fifo_count,
            }
        )
    return pd.DataFrame(rows).sort_values("cluster_id").reset_index(drop=True)


def _node_output(features: pd.DataFrame) -> pd.DataFrame:
    frame = features.copy()
    frame["pagerank"] = frame["pagerank_amount"]
    first = [
        "gid",
        "role",
        "role_score",
        "cluster_id",
        "priority_score",
        "evidence",
    ]
    extras = [
        "secondary_role",
        "uncertainty_reason",
        "depth",
        "is_seed",
        "truncated_by_depth",
        "p_continue",
        "in_deg",
        "out_deg",
        "in_tx",
        "out_tx",
        "in_kzt",
        "out_kzt",
        "pagerank",
        "pass_through",
        "retention",
        "balance_similarity",
        "fifo_0d",
        "fifo_1d",
        "fifo_2d",
        "median_fifo_lag",
        "pagerank_amount",
        "pagerank_count",
        "hits_authority",
        "hits_hub",
        "betweenness",
        "weighted_betweenness",
        "direct_seed_in",
        "seed_affinity",
        "n_seed_reachable",
        "max_in_sources_day",
        "max_out_targets_day",
        "repeated_in_edges",
        "repeated_out_edges",
        "repeat_route_count",
        "reciprocal_neighbors",
        "cycle_count",
        "temporal_cycle_count",
        "participation",
        "fragmentation_impact",
        "cluster_stability",
        "component_id",
        "role_stability",
        "observability",
        *[f"score_{role}" for role in ROLES],
        "priority_connectivity",
        "priority_exposure",
        "priority_seed",
        "priority_temporal",
        "priority_role",
        "priority_uncertainty",
    ]
    result = frame[first + extras].copy()
    result["gid"] = result["gid"].astype(str)
    result["median_fifo_lag"] = result["median_fifo_lag"].fillna(-1.0)
    result["pass_through"] = result["pass_through"].fillna(0.0)
    numeric = result.select_dtypes(include=[np.number]).columns
    result[numeric] = result[numeric].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    result["uncertainty_reason"] = result["uncertainty_reason"].fillna("не определено")
    return result


def _top_nodes(features: pd.DataFrame, top_n: int) -> pd.DataFrame:
    selected = features.sort_values(
        ["priority_score", "priority_raw", "gid"], ascending=[False, False, True]
    ).head(max(20, int(top_n)))
    rows: list[dict[str, object]] = []
    for rank, (_, row) in enumerate(selected.iterrows(), start=1):
        why = priority_why(row)
        rows.append(
            {
                "rank": rank,
                "gid": str(int(row.gid)),
                "role": str(row.role),
                "priority_score": float(row.priority_score),
                "why": why,
                "role_score": float(row.role_score),
                "cluster_id": int(row.cluster_id),
                "secondary_role": str(row.secondary_role),
                "uncertainty_reason": str(row.uncertainty_reason),
            }
        )
    return pd.DataFrame(rows)


def run_pipeline(config: PipelineConfig) -> dict[str, object]:
    started = time.perf_counter()
    timings: dict[str, float] = {}
    config.output_dir.mkdir(parents=True, exist_ok=True)

    checkpoint = time.perf_counter()
    inputs: InputData = load_inputs(config.data_dir)
    graph = build_graph(inputs.nodes, inputs.edges)
    timings["load_and_validate_seconds"] = time.perf_counter() - checkpoint

    checkpoint = time.perf_counter()
    features, motif_metadata = assemble_base_features(
        graph,
        inputs.nodes,
        inputs.edges,
        inputs.transactions,
    )
    timings["features_seconds"] = time.perf_counter() - checkpoint

    checkpoint = time.perf_counter()
    cluster_lookup, cluster_stability, cluster_metadata, communities = clustering.louvain_ensemble(
        graph,
        n_runs=config.cluster_runs,
        random_seed=config.random_seed,
    )
    features = features.merge(cluster_stability, on="gid", how="left", validate="one_to_one")
    features = clustering.add_cluster_graph_features(graph, features, cluster_lookup)
    timings["clustering_seconds"] = time.perf_counter() - checkpoint

    checkpoint = time.perf_counter()
    features, scoring_metadata = role_and_priority_scores(
        features,
        random_seed=config.random_seed,
        stability_runs=config.role_stability_runs,
    )
    timings["roles_and_priority_seconds"] = time.perf_counter() - checkpoint

    checkpoint = time.perf_counter()
    nodes_roles = _node_output(features)
    clusters = _cluster_table(features, inputs.edges, communities)
    top_nodes = _top_nodes(features, config.top_n)
    resilience = resilience_analysis(
        graph,
        features,
        random_seed=config.random_seed,
    )
    timings["resilience_and_tables_seconds"] = time.perf_counter() - checkpoint

    checkpoint = time.perf_counter()
    tables = {"nodes_roles.csv": nodes_roles, "clusters.csv": clusters, "top_nodes.csv": top_nodes}
    for name, table in tables.items():
        table.to_csv(config.output_dir / name.replace(".csv", "_extended.csv"), index=False, encoding="utf-8-sig")
        tables[name] = table.loc[:, list(CSV_COLUMNS[name])].copy()
        tables[name].to_csv(config.output_dir / name, index=False, encoding="utf-8-sig")
    nodes_roles, clusters, top_nodes = (tables[name] for name in CSV_COLUMNS)
    resilience.to_csv(config.output_dir / "resilience.csv", index=False, encoding="utf-8-sig")
    export_features = features.copy()
    export_features["gid"] = export_features["gid"].astype(str)
    export_features.to_csv(config.output_dir / "features.csv", index=False, encoding="utf-8-sig")

    thresholds_payload = {
        **scoring_metadata,
        "clustering": cluster_metadata,
        "motifs": motif_metadata,
        "sampling_constraints": [
            "four-hop outgoing-only crawl",
            "depth-4 outflow is right-censored",
            "transactions below 5000 KZT are absent",
            "seed inflow from outside the crawl is incomplete",
            "daily dates do not identify within-day order",
        ],
    }
    _write_json(config.output_dir / "thresholds.json", thresholds_payload)
    _write_json(config.output_dir / "decision_traces.json", {
        str(row["gid"]): decision_trace(row, scoring_metadata) for row in features.to_dict("records")
    })

    validation = validate_outputs(inputs.nodes, nodes_roles, clusters, top_nodes)
    _write_json(config.output_dir / "validation_report.json", validation)
    timings["write_and_validate_seconds"] = time.perf_counter() - checkpoint
    timings["total_seconds"] = time.perf_counter() - started

    metadata: dict[str, object] = {
        "status": "ok" if validation["passed"] else "validation_failed",
        "methodology": "deterministic_explainable_data_only",
        "interpretation": "Роли и приоритеты — гипотезы для проверки аналитиком, не вывод о виновности.",
        "config": asdict(config),
        "input_checks": inputs.checks,
        "graph": {
            "n_nodes": graph.number_of_nodes(),
            "n_edges": graph.number_of_edges(),
            "n_weak_components": nx.number_weakly_connected_components(graph),
            "largest_weak_component": max(
                (len(component) for component in nx.weakly_connected_components(graph)), default=0
            ),
        },
        "role_counts": features["role"].value_counts().to_dict(),
        "cluster_metadata": cluster_metadata,
        "motif_metadata": motif_metadata,
        "timings": timings,
        "runtime": {
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "networkx": nx.__version__,
            "numpy": np.__version__,
        },
        "outputs": [
            "nodes_roles_extended.csv",
            "clusters_extended.csv",
            "top_nodes_extended.csv",
            "nodes_roles.csv",
            "clusters.csv",
            "top_nodes.csv",
            "resilience.csv",
            "features.csv",
            "thresholds.json",
            "decision_traces.json",
            "validation_report.json",
            "run_metadata.json",
        ],
    }
    _write_json(config.output_dir / "run_metadata.json", metadata)
    if config.validate and not validation["passed"]:
        failed = [
            key for key, passed in validation["schema_and_invariants"].items() if not passed
        ]
        raise RuntimeError("Валидация не пройдена: " + ", ".join(failed))
    return metadata
