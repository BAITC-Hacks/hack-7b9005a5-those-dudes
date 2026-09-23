#!/usr/bin/env python3
"""Build the self-contained data bundle used by the HackAlem dashboard.

The resulting dashboard_data.js is served by the local backend for the matching
dataset run. Building the bundle does not contact external services.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from money_graph.io import load_inputs  # noqa: E402

DEFAULT_DATA = ROOT / "input_data" / "data"
DEFAULT_DIST = Path(__file__).resolve().parent / "dist"
OPTIONAL_OUTPUT = ROOT / "output"


def clean(value: Any) -> Any:
    """Convert numpy/pandas values to JSON-safe native values."""
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not math.isfinite(float(value)) else round(float(value), 10)
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return str(pd.Timestamp(value).date())
    return value


def records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [{str(k): clean(v) for k, v in row.items()} for row in df.to_dict("records")]


def json_safe(value: Any) -> Any:
    """Recursively convert optional JSON/CSV output values for the browser bundle."""
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return clean(value)


def browser_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Serialize a frame while preserving every identifier as a string."""
    frame = df.copy()
    identifier_columns = {
        "gid", "src", "dst", "source", "target", "node_id", "top_gid",
    }
    def identifier_string(value: Any) -> str | None:
        if pd.isna(value):
            return None
        if isinstance(value, (int, np.integer)):
            return str(int(value))
        if isinstance(value, (float, np.floating)) and math.isfinite(float(value)) and float(value).is_integer():
            return str(int(value))
        return str(value)

    for column in identifier_columns & set(frame.columns):
        frame[column] = frame[column].map(identifier_string)
    return records(frame)


def read_optional_csv(path: Path, warnings: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        identifiers = {"gid", "src", "dst", "source", "target", "node_id", "top_gid", "top_gids"}
        columns = pd.read_csv(path, nrows=0).columns
        return pd.read_csv(path, dtype={column: "string" for column in identifiers & set(columns)})
    except Exception as error:  # Optional analytical artifacts must not break raw views.
        warnings.append(f"{path.name}: {error}")
        return pd.DataFrame()


def read_optional_json(path: Path, warnings: list[str]) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        warnings.append(f"{path.name}: {error}")
        return None


def percentile_rank(series: pd.Series) -> pd.Series:
    return series.rank(method="average", pct=True).fillna(0.0)


def fifo_match(in_daily: dict[pd.Timestamp, float], out_daily: dict[pd.Timestamp, float], lag_days: int) -> float:
    """Match each unit of observed outflow to recent observed inflow at most once."""
    dates = sorted(set(in_daily) | set(out_daily))
    lots: deque[list[Any]] = deque()
    matched = 0.0
    for date in dates:
        cutoff = date - pd.Timedelta(days=lag_days)
        while lots and lots[0][0] < cutoff:
            lots.popleft()
        incoming = float(in_daily.get(date, 0.0))
        if incoming > 0:
            lots.append([date, incoming])
        outgoing = float(out_daily.get(date, 0.0))
        while outgoing > 0 and lots:
            taken = min(outgoing, lots[0][1])
            matched += taken
            outgoing -= taken
            lots[0][1] -= taken
            if lots[0][1] <= 1e-9:
                lots.popleft()
    return matched


def stable_components(graph: nx.DiGraph) -> tuple[dict[int, int], list[dict[str, Any]]]:
    groups = sorted(nx.weakly_connected_components(graph), key=lambda x: (-len(x), min(x)))
    lookup: dict[int, int] = {}
    rows: list[dict[str, Any]] = []
    for component_id, group in enumerate(groups):
        for gid in group:
            lookup[int(gid)] = component_id
        rows.append({"component_id": component_id, "n_nodes": len(group)})
    return lookup, rows


def stable_louvain(graph: nx.DiGraph) -> tuple[dict[int, int], list[dict[str, Any]]]:
    undirected = nx.Graph()
    undirected.add_nodes_from(graph.nodes)
    for source, target, attrs in graph.edges(data=True):
        weight = math.log1p(float(attrs["sum_kzt"]) / 5_000.0) + 0.25 * math.log1p(
            float(attrs["n_tx"])
        )
        if undirected.has_edge(source, target):
            undirected[source][target]["weight"] += weight
        else:
            undirected.add_edge(source, target, weight=weight)

    communities = nx.community.louvain_communities(
        undirected, weight="weight", resolution=1.0, seed=42
    )
    communities = sorted(communities, key=lambda x: (-len(x), min(x)))
    lookup: dict[int, int] = {}
    rows: list[dict[str, Any]] = []
    for cluster_id, community in enumerate(communities):
        for gid in community:
            lookup[int(gid)] = cluster_id
        rows.append({"cluster_id": cluster_id, "n_nodes": len(community)})
    return lookup, rows


def build_layouts(features: pd.DataFrame) -> pd.DataFrame:
    """Create deterministic depth and cluster layouts without O(N²) simulation."""
    result = features.copy()

    depth_positions: dict[int, tuple[float, float]] = {}
    for depth, frame in result.groupby("depth", sort=True):
        frame = frame.sort_values(
            ["component_id", "cluster_id", "is_seed", "pagerank_amount", "gid"],
            ascending=[True, True, False, False, True],
        )
        count = len(frame)
        for order, gid in enumerate(frame.gid):
            jitter = ((int(gid) * 2654435761) % 997) / 9970.0 - 0.05
            x = float(depth) / 4.0 + jitter
            y = 0.5 if count == 1 else order / (count - 1)
            depth_positions[int(gid)] = (x, y)

    cluster_ids = sorted(result.cluster_id.unique())
    n_clusters = max(1, len(cluster_ids))
    cluster_centres: dict[int, tuple[float, float]] = {}
    columns = max(1, math.ceil(math.sqrt(n_clusters * 1.55)))
    rows = math.ceil(n_clusters / columns)
    for index, cluster_id in enumerate(cluster_ids):
        col = index % columns
        row = index // columns
        cluster_centres[int(cluster_id)] = (
            (col + 0.5) / columns,
            (row + 0.5) / rows,
        )

    cluster_positions: dict[int, tuple[float, float]] = {}
    for cluster_id, frame in result.groupby("cluster_id", sort=True):
        frame = frame.sort_values(["depth", "pagerank_amount", "gid"], ascending=[True, False, True])
        centre_x, centre_y = cluster_centres[int(cluster_id)]
        n = len(frame)
        cell_radius = min(0.38 / columns, 0.38 / rows)
        for order, row in enumerate(frame.itertuples(index=False)):
            angle = 2 * math.pi * order / max(n, 1) + int(row.depth) * 0.42
            ring = 0.22 + 0.72 * math.sqrt((order + 1) / max(n, 1))
            cluster_positions[int(row.gid)] = (
                centre_x + math.cos(angle) * cell_radius * ring,
                centre_y + math.sin(angle) * cell_radius * ring,
            )

    result["x_depth"] = result.gid.map(lambda gid: depth_positions[int(gid)][0])
    result["y_depth"] = result.gid.map(lambda gid: depth_positions[int(gid)][1])
    result["x_cluster"] = result.gid.map(lambda gid: cluster_positions[int(gid)][0])
    result["y_cluster"] = result.gid.map(lambda gid: cluster_positions[int(gid)][1])
    return result


def optional_roles(features: pd.DataFrame, roles: pd.DataFrame) -> pd.DataFrame:
    """Merge production node outputs without allowing duplicate raw fields to corrupt data."""
    result = features.copy()
    defaults: dict[str, Any] = {
        "role": "",
        "role_score": np.nan,
        "priority_score": np.nan,
        "evidence": "",
        "secondary_role": "",
        "uncertainty_reason": "",
    }
    if roles.empty or "gid" not in roles.columns:
        for column, value in defaults.items():
            result[column] = value
        return result

    role_frame = roles.copy()
    role_frame["_gid_key"] = role_frame["gid"].map(
        lambda value: "" if pd.isna(value) else str(value).removesuffix(".0")
    )
    role_frame = role_frame[role_frame._gid_key != ""].drop_duplicates("_gid_key", keep="last")
    role_frame = role_frame.drop(columns=["gid"])
    result["_gid_key"] = result.gid.astype(str)

    rename: dict[str, str] = {}
    drop: list[str] = []
    for column in role_frame.columns:
        if column == "_gid_key":
            continue
        if column == "cluster_id":
            rename[column] = "production_cluster_id"
        elif column in result.columns:
            # Keep the independently computed raw metric and expose a differing
            # production value explicitly instead of creating pandas _x/_y fields.
            if column in {"role", "role_score", "priority_score", "evidence"}:
                rename[column] = f"production_{column}"
            else:
                drop.append(column)
    if drop:
        role_frame = role_frame.drop(columns=drop)
    if rename:
        role_frame = role_frame.rename(columns=rename)
    result = result.merge(role_frame, on="_gid_key", how="left", validate="one_to_one")

    for column in ("role", "role_score", "priority_score", "evidence"):
        production_column = f"production_{column}"
        if production_column in result.columns:
            result[column] = result[production_column]
            result = result.drop(columns=[production_column])
    if "production_cluster_id" in result.columns:
        production_cluster = pd.to_numeric(result.production_cluster_id, errors="coerce")
        result["cluster_id"] = production_cluster.fillna(result.cluster_id).astype(int)
    for column, value in defaults.items():
        if column not in result.columns:
            result[column] = value
        elif isinstance(value, str):
            result[column] = result[column].fillna("")
    return result.drop(columns=["_gid_key"])


def build(data_dir: Path, dist_dir: Path, output_dir: Path) -> Path:
    # Match exactly the normalized values used by the pipeline. In particular,
    # do not treat the string "False" as true or lose precision in large GIDs.
    inputs = load_inputs(data_dir)
    nodes, edges, transactions = inputs.nodes, inputs.edges, inputs.transactions

    optional_warnings: list[str] = []
    role_output = read_optional_csv(output_dir / "nodes_roles.csv", optional_warnings)
    cluster_output = read_optional_csv(output_dir / "clusters.csv", optional_warnings)
    top_output = read_optional_csv(output_dir / "top_nodes.csv", optional_warnings)
    resilience_output = read_optional_csv(output_dir / "resilience.csv", optional_warnings)
    features_output = read_optional_csv(output_dir / "features.csv", optional_warnings)
    thresholds_output = read_optional_json(output_dir / "thresholds.json", optional_warnings)
    run_metadata_output = read_optional_json(output_dir / "run_metadata.json", optional_warnings)
    validation_output = None
    validation_source = None
    for filename in ("validation.json", "validation_report.json"):
        candidate = read_optional_json(output_dir / filename, optional_warnings)
        if candidate is not None:
            validation_output = candidate
            validation_source = filename
            break
    node_details_output = read_optional_json(output_dir / "node_details.json", optional_warnings)

    required = {
        "nodes": {"gid", "depth", "is_seed"},
        "edges": {"src", "dst", "sum_kzt", "n_tx", "depth"},
        "transactions": {"src", "dst", "date", "sum_kzt"},
    }
    for name, frame in [("nodes", nodes), ("edges", edges), ("transactions", transactions)]:
        missing = required[name] - set(frame.columns)
        if missing:
            raise ValueError(f"{name}.parquet is missing columns: {sorted(missing)}")

    graph = nx.from_pandas_edgelist(
        edges,
        source="src",
        target="dst",
        edge_attr=["sum_kzt", "n_tx", "depth"],
        create_using=nx.DiGraph,
    )
    graph.add_nodes_from(nodes.gid.astype(int))

    aggregated = (
        transactions.groupby(["src", "dst"], as_index=False)
        .agg(check_sum=("sum_kzt", "sum"), check_tx=("sum_kzt", "size"))
        .merge(edges, on=["src", "dst"], how="outer", indicator=True)
    )
    reconciliation_ok = bool(
        (aggregated["_merge"] == "both").all()
        and np.allclose(aggregated.check_sum, aggregated.sum_kzt)
        and (aggregated.check_tx == aggregated.n_tx).all()
    )

    features = nodes.copy()
    features["in_deg"] = features.gid.map(dict(graph.in_degree())).fillna(0).astype(int)
    features["out_deg"] = features.gid.map(dict(graph.out_degree())).fillna(0).astype(int)
    features["in_kzt"] = features.gid.map(dict(graph.in_degree(weight="sum_kzt"))).fillna(0.0)
    features["out_kzt"] = features.gid.map(dict(graph.out_degree(weight="sum_kzt"))).fillna(0.0)
    features["in_tx"] = features.gid.map(dict(graph.in_degree(weight="n_tx"))).fillna(0).astype(int)
    features["out_tx"] = features.gid.map(dict(graph.out_degree(weight="n_tx"))).fillna(0).astype(int)
    features["total_kzt"] = features.in_kzt + features.out_kzt
    features["pass_through"] = np.where(
        features.in_kzt > 0, features.out_kzt / features.in_kzt, np.nan
    )
    features["truncated_by_depth"] = (features.depth == 4) & (features.out_deg == 0)
    features["isolated"] = (features.in_deg == 0) & (features.out_deg == 0)

    pagerank_amount = nx.pagerank(graph, alpha=0.85, weight="sum_kzt")
    pagerank_count = nx.pagerank(graph, alpha=0.85, weight="n_tx")
    features["pagerank_amount"] = features.gid.map(pagerank_amount).fillna(0.0)
    features["pagerank_count"] = features.gid.map(pagerank_count).fillna(0.0)

    try:
        if graph.number_of_nodes() < 2:
            raise ValueError("Sparse HITS requires at least two nodes")
        hubs, authorities = nx.hits(graph, max_iter=1_000, normalized=True)
    except (nx.PowerIterationFailedConvergence, ValueError):
        hubs = dict(graph.out_degree())
        authorities = dict(graph.in_degree())
        hub_total, authority_total = sum(hubs.values()) or 1, sum(authorities.values()) or 1
        hubs = {gid: value / hub_total for gid, value in hubs.items()}
        authorities = {gid: value / authority_total for gid, value in authorities.items()}
    features["hits_hub"] = features.gid.map(hubs).fillna(0.0).clip(lower=0)
    features["hits_authority"] = features.gid.map(authorities).fillna(0.0).clip(lower=0)

    for source, target, attrs in graph.edges(data=True):
        attrs["distance"] = 1.0 / max(math.log1p(float(attrs["sum_kzt"]) / 5_000.0), 1e-9)
    betweenness = nx.betweenness_centrality(graph, normalized=True, weight="distance")
    features["betweenness"] = features.gid.map(betweenness).fillna(0.0)

    component_lookup, components = stable_components(graph)
    cluster_lookup, baseline_clusters = stable_louvain(graph)
    features["component_id"] = features.gid.map(component_lookup).astype(int)
    features["baseline_cluster_id"] = features.gid.map(cluster_lookup).astype(int)
    features["cluster_id"] = features.gid.map(cluster_lookup).astype(int)

    seed_set = set(nodes.loc[nodes.is_seed, "gid"].astype(int))
    direct_seed_sources: dict[int, set[int]] = defaultdict(set)
    for source, target in graph.edges:
        if int(source) in seed_set:
            direct_seed_sources[int(target)].add(int(source))
    features["direct_seed_in"] = features.gid.map(
        lambda gid: len(direct_seed_sources.get(int(gid), set()))
    )

    tx_in_days = transactions.groupby("dst").date.nunique()
    tx_out_days = transactions.groupby("src").date.nunique()
    features["in_active_days"] = features.gid.map(tx_in_days).fillna(0).astype(int)
    features["out_active_days"] = features.gid.map(tx_out_days).fillna(0).astype(int)

    incoming_daily: dict[int, dict[pd.Timestamp, float]] = defaultdict(lambda: defaultdict(float))
    outgoing_daily: dict[int, dict[pd.Timestamp, float]] = defaultdict(lambda: defaultdict(float))
    incoming_sources_daily: dict[int, dict[pd.Timestamp, set[int]]] = defaultdict(lambda: defaultdict(set))
    outgoing_targets_daily: dict[int, dict[pd.Timestamp, set[int]]] = defaultdict(lambda: defaultdict(set))
    for row in transactions.itertuples(index=False):
        source, target, date, amount = int(row.src), int(row.dst), pd.Timestamp(row.date), float(row.sum_kzt)
        incoming_daily[target][date] += amount
        outgoing_daily[source][date] += amount
        incoming_sources_daily[target][date].add(source)
        outgoing_targets_daily[source][date].add(target)

    fifo_0d: dict[int, float] = {}
    fifo_1d: dict[int, float] = {}
    max_in_sources_day: dict[int, int] = {}
    max_out_targets_day: dict[int, int] = {}
    for gid in features.gid.astype(int):
        denominator = min(sum(incoming_daily[gid].values()), sum(outgoing_daily[gid].values()))
        fifo_0d[gid] = fifo_match(incoming_daily[gid], outgoing_daily[gid], 0) / denominator if denominator > 0 else np.nan
        fifo_1d[gid] = fifo_match(incoming_daily[gid], outgoing_daily[gid], 1) / denominator if denominator > 0 else np.nan
        max_in_sources_day[gid] = max((len(group) for group in incoming_sources_daily[gid].values()), default=0)
        max_out_targets_day[gid] = max((len(group) for group in outgoing_targets_daily[gid].values()), default=0)
    features["fifo_match_0d"] = features.gid.map(fifo_0d)
    features["fifo_match_1d"] = features.gid.map(fifo_1d)
    features["max_in_sources_day"] = features.gid.map(max_in_sources_day).fillna(0).astype(int)
    features["max_out_targets_day"] = features.gid.map(max_out_targets_day).fillna(0).astype(int)

    features["rank_in"] = (
        percentile_rank(np.log1p(features.in_deg))
        + percentile_rank(np.log1p(features.in_tx))
        + percentile_rank(np.log1p(features.in_kzt))
    ) / 3.0
    features["rank_out"] = (
        percentile_rank(np.log1p(features.out_deg))
        + percentile_rank(np.log1p(features.out_tx))
        + percentile_rank(np.log1p(features.out_kzt))
    ) / 3.0
    features = optional_roles(features, role_output)
    features = build_layouts(features)

    edge_index = edges.copy()
    edge_index["reciprocal"] = [graph.has_edge(int(row.dst), int(row.src)) for row in edges.itertuples()]
    edge_index["share_of_total"] = edge_index.sum_kzt / edge_index.sum_kzt.sum()

    daily = (
        transactions.groupby("date", as_index=False)
        .agg(sum_kzt=("sum_kzt", "sum"), n_tx=("sum_kzt", "size"), n_senders=("src", "nunique"), n_receivers=("dst", "nunique"))
        .sort_values("date")
    )
    depth_summary = (
        features.groupby("depth", as_index=False)
        .agg(
            n_nodes=("gid", "size"),
            n_seed=("is_seed", "sum"),
            in_kzt=("in_kzt", "sum"),
            out_kzt=("out_kzt", "sum"),
            mean_pagerank=("pagerank_amount", "mean"),
        )
        .sort_values("depth")
    )

    component_nodes = features.groupby("component_id").gid.count().to_dict()
    component_seeds = features.groupby("component_id").is_seed.sum().to_dict()
    component_edges = edges.assign(
        component_id=edges.src.map(component_lookup)
    ).groupby("component_id").agg(n_edges=("src", "size"), sum_kzt=("sum_kzt", "sum"))
    for row in components:
        cid = row["component_id"]
        row.update(
            {
                "n_nodes": int(component_nodes.get(cid, 0)),
                "n_seed": int(component_seeds.get(cid, 0)),
                "n_edges": int(component_edges.n_edges.get(cid, 0)),
                "sum_kzt": float(component_edges.sum_kzt.get(cid, 0.0)),
            }
        )

    final_cluster_lookup = features.set_index("gid").cluster_id.astype(int).to_dict()
    cluster_nodes = features.groupby("cluster_id").agg(
        n_nodes=("gid", "size"),
        n_seed=("is_seed", "sum"),
        mean_depth=("depth", "mean"),
        top_pagerank=("pagerank_amount", "max"),
    )
    cluster_edges = edges.assign(cluster_id=edges.src.map(final_cluster_lookup))
    cluster_internal = cluster_edges[
        cluster_edges.cluster_id == cluster_edges.dst.map(final_cluster_lookup)
    ].groupby("cluster_id").agg(n_internal_edges=("src", "size"), internal_kzt=("sum_kzt", "sum"))
    clusters: list[dict[str, Any]] = [
        {"cluster_id": int(cluster_id)} for cluster_id in sorted(features.cluster_id.unique())
    ]
    for row in clusters:
        cid = row["cluster_id"]
        row.update(
            {
                "n_nodes": int(cluster_nodes.n_nodes.get(cid, 0)),
                "n_seed": int(cluster_nodes.n_seed.get(cid, 0)),
                "mean_depth": float(cluster_nodes.mean_depth.get(cid, 0.0)),
                "n_internal_edges": int(cluster_internal.n_internal_edges.get(cid, 0)),
                "internal_kzt": float(cluster_internal.internal_kzt.get(cid, 0.0)),
            }
        )

    if not cluster_output.empty and "cluster_id" in cluster_output.columns:
        production_clusters = cluster_output.copy()
        production_clusters["cluster_id"] = pd.to_numeric(
            production_clusters.cluster_id, errors="coerce"
        )
        production_clusters = production_clusters.dropna(subset=["cluster_id"])
        production_clusters["cluster_id"] = production_clusters.cluster_id.astype(int)
        production_lookup = {
            int(row["cluster_id"]): row
            for row in production_clusters.to_dict("records")
        }
        for row in clusters:
            production_row = production_lookup.get(row["cluster_id"], {})
            for key, value in production_row.items():
                if key == "cluster_id":
                    continue
                try:
                    missing = bool(pd.isna(value))
                except (TypeError, ValueError):
                    missing = False
                if not missing:
                    row[key] = clean(value)

    quantile_columns = [
        "in_deg", "out_deg", "in_tx", "out_tx", "in_kzt", "out_kzt",
        "pagerank_amount", "betweenness",
    ]
    quantiles: dict[str, dict[str, float]] = {}
    for column in quantile_columns:
        quantiles[column] = {
            f"q{int(probability * 100)}": clean(features[column].quantile(probability))
            for probability in (0.5, 0.75, 0.9, 0.95, 0.99)
        }

    meta = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "period_start": clean(transactions.date.min()),
        "period_end": clean(transactions.date.max()),
        "n_nodes": len(nodes),
        "n_edges": len(edges),
        "n_transactions": len(transactions),
        "turnover_kzt": float(transactions.sum_kzt.sum()),
        "n_seed": int(nodes.is_seed.sum()),
        "n_isolated": int(features.isolated.sum()),
        "n_censored": int(features.truncated_by_depth.sum()),
        "n_both_flow": int(((features.in_deg > 0) & (features.out_deg > 0)).sum()),
        "n_components": len(components),
        "n_edge_components": sum(row["n_edges"] > 0 for row in components),
        "largest_component": max(row["n_nodes"] for row in components),
        "n_clusters": len(clusters),
        "n_baseline_clusters": len(baseline_clusters),
        "n_reciprocal_pairs": int(edge_index.reciprocal.sum() // 2),
        "n_repeated_edges": int((edges.n_tx >= 2).sum()),
        "n_multi_seed_direct": int((features.direct_seed_in >= 2).sum()),
        "n_fifo_0d_80": int((features.fifo_match_0d >= 0.8).sum()),
        "n_fifo_1d_80": int((features.fifo_match_1d >= 0.8).sum()),
        "n_sync_fanin_3": int((features.max_in_sources_day >= 3).sum()),
        "n_sync_fanout_5": int((features.max_out_targets_day >= 5).sum()),
        "reconciliation_ok": reconciliation_ok,
        "has_roles": bool(not role_output.empty and "role" in role_output.columns),
        "has_production_clusters": bool(not cluster_output.empty),
        "has_top_nodes": bool(not top_output.empty),
        "has_resilience": bool(not resilience_output.empty),
        "has_validation": validation_output is not None,
    }

    # Browser JavaScript cannot represent the ~1e17 gids exactly as numbers.
    # Preserve identifiers losslessly as strings in every browser-facing table.
    browser_features = features.copy()
    browser_features["gid"] = browser_features.gid.astype(str)
    browser_edges = edge_index.copy()
    browser_edges["src"] = browser_edges.src.astype(str)
    browser_edges["dst"] = browser_edges.dst.astype(str)
    browser_transactions = transactions.sort_values(["date", "src", "dst"]).copy()
    browser_transactions["src"] = browser_transactions.src.astype(str)
    browser_transactions["dst"] = browser_transactions.dst.astype(str)

    known_json = {
        "thresholds.json", "run_metadata.json", "validation.json",
        "validation_report.json", "node_details.json",
    }
    extra_json: dict[str, Any] = {}
    for path in sorted(output_dir.glob("*.json")) if output_dir.exists() else []:
        if path.name in known_json:
            continue
        value = read_optional_json(path, optional_warnings)
        if value is not None:
            extra_json[path.stem] = json_safe(value)

    known_csv = {
        "nodes_roles.csv", "clusters.csv", "top_nodes.csv",
        "resilience.csv", "features.csv",
    }
    extra_csv: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(output_dir.glob("*.csv")) if output_dir.exists() else []:
        if path.name in known_csv:
            continue
        frame = read_optional_csv(path, optional_warnings)
        if not frame.empty:
            extra_csv[path.stem] = browser_records(frame)

    production = {
        "available": bool(
            not role_output.empty
            or not cluster_output.empty
            or not top_output.empty
            or not resilience_output.empty
            or validation_output is not None
        ),
        "files": {
            "nodes_roles.csv": bool(not role_output.empty),
            "clusters.csv": bool(not cluster_output.empty),
            "top_nodes.csv": bool(not top_output.empty),
            "resilience.csv": bool(not resilience_output.empty),
            "features.csv": bool(not features_output.empty),
            "thresholds.json": thresholds_output is not None,
            "run_metadata.json": run_metadata_output is not None,
            validation_source or "validation.json": validation_output is not None,
            "node_details.json": node_details_output is not None,
        },
        "role_columns": [str(column) for column in role_output.columns],
        "top_nodes": browser_records(top_output),
        "clusters": browser_records(cluster_output),
        "resilience": browser_records(resilience_output),
        "features": browser_records(features_output),
        "thresholds": json_safe(thresholds_output),
        "run_metadata": json_safe(run_metadata_output),
        "validation": json_safe(validation_output),
        "node_details": json_safe(node_details_output),
        "extra_json": extra_json,
        "extra_csv": extra_csv,
        "warnings": optional_warnings,
    }

    payload = {
        "meta": meta,
        "nodes": records(browser_features),
        "edges": records(browser_edges),
        "transactions": records(browser_transactions),
        "daily": records(daily),
        "depth_summary": records(depth_summary),
        "components": [{k: clean(v) for k, v in row.items()} for row in components],
        "clusters": [{k: clean(v) for k, v in row.items()} for row in clusters],
        "baseline_clusters": [
            {k: clean(v) for k, v in row.items()} for row in baseline_clusters
        ],
        "quantiles": quantiles,
        "production": production,
        "dictionary": {
            "nodes": {
                "gid": "Обезличенный идентификатор узла",
                "depth": "Минимальная глубина исходящего обхода",
                "is_seed": "Узел входит в исходный seed-список",
                "truncated_by_depth": "Depth=4 и исходящие рёбра не наблюдаются",
                "pagerank_amount": "PageRank, взвешенный суммой переводов",
                "betweenness": "Directed weighted betweenness",
            }
        },
    }

    dist_dir.mkdir(parents=True, exist_ok=True)
    output_path = dist_dir / "dashboard_data.js"
    output_path.write_text(
        "window.HACKALEM_DATA=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )
    print(
        f"Built {output_path} | {meta['n_nodes']} nodes, {meta['n_edges']} edges, "
        f"{meta['n_transactions']} transactions"
    )
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build HackAlem dashboard data")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--dist", type=Path, default=DEFAULT_DIST)
    parser.add_argument("--outputs", type=Path, default=OPTIONAL_OUTPUT)
    args = parser.parse_args()
    build(args.data.resolve(), args.dist.resolve(), args.outputs.resolve())


if __name__ == "__main__":
    main()
