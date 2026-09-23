from __future__ import annotations

from bisect import bisect_left

from collections import Counter, defaultdict
from math import exp, log1p
from typing import Iterable

import networkx as nx
import numpy as np
import pandas as pd


def build_graph(nodes: pd.DataFrame, edges: pd.DataFrame) -> nx.DiGraph:
    graph = nx.DiGraph()
    graph.add_nodes_from(int(gid) for gid in nodes["gid"])
    for row in edges.itertuples(index=False):
        amount = float(row.sum_kzt)
        count = int(row.n_tx)
        graph.add_edge(
            int(row.src),
            int(row.dst),
            sum_kzt=amount,
            n_tx=count,
            depth=int(row.depth),
            amount_distance=1.0 / max(log1p(amount / 5_000.0), 1e-9),
        )
    return graph


def _safe_pagerank(graph: nx.DiGraph, weight: str) -> dict[int, float]:
    try:
        return nx.pagerank(graph, alpha=0.85, weight=weight, max_iter=1_000, tol=1e-10)
    except nx.PowerIterationFailedConvergence:
        return nx.pagerank(graph, alpha=0.85, weight=weight, max_iter=5_000, tol=1e-8)


def _hits_for_weight(graph: nx.DiGraph, attribute: str) -> tuple[dict[int, float], dict[int, float]]:
    weighted = nx.DiGraph()
    weighted.add_nodes_from(graph.nodes)
    for source, target, data in graph.edges(data=True):
        weighted.add_edge(source, target, weight=log1p(float(data[attribute])))
    try:
        hubs, authorities = nx.hits(weighted, max_iter=2_000, tol=1e-10, normalized=True)
    except (nx.PowerIterationFailedConvergence, ValueError):
        hubs = {node: float(weighted.out_degree(node, weight="weight")) for node in weighted}
        authorities = {node: float(weighted.in_degree(node, weight="weight")) for node in weighted}
        hub_total = sum(hubs.values()) or 1.0
        authority_total = sum(authorities.values()) or 1.0
        hubs = {node: value / hub_total for node, value in hubs.items()}
        authorities = {node: value / authority_total for node, value in authorities.items()}
    # ARPACK can return tiny signed numerical noise.
    return (
        {node: max(0.0, float(value)) for node, value in hubs.items()},
        {node: max(0.0, float(value)) for node, value in authorities.items()},
    )


def _map(frame: pd.DataFrame, column: str, values: dict[int, float], default: float = 0.0) -> None:
    frame[column] = frame["gid"].map(values).fillna(default).astype(float)


def basic_and_centrality_features(
    graph: nx.DiGraph,
    nodes: pd.DataFrame,
) -> pd.DataFrame:
    frame = nodes[["gid", "depth", "is_seed"]].copy()
    degree_maps = {
        "in_deg": dict(graph.in_degree()),
        "out_deg": dict(graph.out_degree()),
        "in_kzt": dict(graph.in_degree(weight="sum_kzt")),
        "out_kzt": dict(graph.out_degree(weight="sum_kzt")),
        "in_tx": dict(graph.in_degree(weight="n_tx")),
        "out_tx": dict(graph.out_degree(weight="n_tx")),
    }
    for column, mapping in degree_maps.items():
        frame[column] = frame["gid"].map(mapping).fillna(0)
    for column in ("in_deg", "out_deg", "in_tx", "out_tx"):
        frame[column] = frame[column].astype(int)
    for column in ("in_kzt", "out_kzt"):
        frame[column] = frame[column].astype(float)

    frame["total_kzt"] = frame["in_kzt"] + frame["out_kzt"]
    frame["total_tx"] = frame["in_tx"] + frame["out_tx"]
    frame["pass_through"] = np.where(
        frame["in_kzt"] > 0,
        frame["out_kzt"] / frame["in_kzt"].replace(0, np.nan),
        np.nan,
    )
    frame["balance_similarity"] = 0.0
    positive_flow = (frame["in_kzt"] > 0) & (frame["out_kzt"] > 0)
    flow_ratio = (
        frame.loc[positive_flow, "out_kzt"] / frame.loc[positive_flow, "in_kzt"]
    )
    frame.loc[positive_flow, "balance_similarity"] = np.exp(-np.abs(np.log(flow_ratio)))
    frame["retention"] = np.where(
        frame["in_kzt"] > 0,
        np.maximum(frame["in_kzt"] - frame["out_kzt"], 0.0) / frame["in_kzt"],
        0.0,
    )
    frame["in_flow_share"] = np.where(
        (frame["in_kzt"] + frame["out_kzt"]) > 0,
        frame["in_kzt"] / (frame["in_kzt"] + frame["out_kzt"]),
        0.0,
    )
    frame["out_flow_share"] = np.where(
        (frame["in_kzt"] + frame["out_kzt"]) > 0,
        frame["out_kzt"] / (frame["in_kzt"] + frame["out_kzt"]),
        0.0,
    )
    frame["truncated_by_depth"] = (frame["depth"] == 4) & (frame["out_deg"] == 0)

    _map(frame, "pagerank_amount", _safe_pagerank(graph, "sum_kzt"))
    _map(frame, "pagerank_count", _safe_pagerank(graph, "n_tx"))
    hubs_amount, authorities_amount = _hits_for_weight(graph, "sum_kzt")
    hubs_count, authorities_count = _hits_for_weight(graph, "n_tx")
    _map(frame, "hits_hub_amount", hubs_amount)
    _map(frame, "hits_authority_amount", authorities_amount)
    _map(frame, "hits_hub_count", hubs_count)
    _map(frame, "hits_authority_count", authorities_count)
    frame["hits_hub"] = (frame["hits_hub_amount"] + frame["hits_hub_count"]) / 2.0
    frame["hits_authority"] = (
        frame["hits_authority_amount"] + frame["hits_authority_count"]
    ) / 2.0

    unweighted = nx.betweenness_centrality(graph, normalized=True, weight=None)
    weighted = nx.betweenness_centrality(
        graph,
        normalized=True,
        weight="amount_distance",
    )
    _map(frame, "betweenness", unweighted)
    _map(frame, "weighted_betweenness", weighted)
    frame["brokerage"] = (frame["betweenness"] + frame["weighted_betweenness"]) / 2.0
    return frame


def _hhi(values: Iterable[float]) -> float:
    numbers = np.asarray(list(values), dtype=float)
    total = float(numbers.sum())
    if total <= 0:
        return 0.0
    shares = numbers / total
    return float(np.square(shares).sum())


def edge_distribution_features(edges: pd.DataFrame) -> pd.DataFrame:
    incoming_hhi = edges.groupby("dst")["sum_kzt"].apply(_hhi).to_dict()
    outgoing_hhi = edges.groupby("src")["sum_kzt"].apply(_hhi).to_dict()
    repeated_in = edges.assign(repeated=edges["n_tx"] >= 2).groupby("dst")["repeated"].sum().to_dict()
    repeated_out = edges.assign(repeated=edges["n_tx"] >= 2).groupby("src")["repeated"].sum().to_dict()
    gids = sorted(set(edges["src"]) | set(edges["dst"]))
    return pd.DataFrame(
        {
            "gid": gids,
            "in_source_hhi": [float(incoming_hhi.get(gid, 0.0)) for gid in gids],
            "out_target_hhi": [float(outgoing_hhi.get(gid, 0.0)) for gid in gids],
            "repeated_in_edges": [int(repeated_in.get(gid, 0)) for gid in gids],
            "repeated_out_edges": [int(repeated_out.get(gid, 0)) for gid in gids],
        }
    )


def _fifo_match(
    incoming: list[tuple[pd.Timestamp, float]],
    outgoing: list[tuple[pd.Timestamp, float]],
    max_lag_days: int,
) -> tuple[float, float]:
    """FIFO amount match; every inbound unit can be used at most once."""

    remaining = [[date, float(amount)] for date, amount in sorted(incoming)]
    matched = 0.0
    lag_weights: list[tuple[int, float]] = []
    for out_date, raw_out_amount in sorted(outgoing):
        needed = float(raw_out_amount)
        for item in remaining:
            in_date, available = item
            lag = int((out_date - in_date).days)
            if lag < 0 or lag > max_lag_days or available <= 0 or needed <= 0:
                continue
            used = min(available, needed)
            item[1] -= used
            needed -= used
            matched += used
            lag_weights.append((lag, used))
        if needed <= 0:
            continue
    if not lag_weights:
        return matched, float("nan")
    half = sum(weight for _, weight in lag_weights) / 2.0
    cumulative = 0.0
    median_lag = 0.0
    for lag, weight in sorted(lag_weights):
        cumulative += weight
        if cumulative >= half:
            median_lag = float(lag)
            break
    return matched, median_lag


def temporal_features(nodes: pd.DataFrame, tx: pd.DataFrame) -> pd.DataFrame:
    daily = (
        tx.groupby(["src", "dst", "date"], as_index=False, sort=False)
        .agg(sum_kzt=("sum_kzt", "sum"), n_tx=("sum_kzt", "size"))
    )
    incoming_daily = (
        tx.groupby(["dst", "date"], as_index=False)["sum_kzt"].sum().groupby("dst")
    )
    outgoing_daily = (
        tx.groupby(["src", "date"], as_index=False)["sum_kzt"].sum().groupby("src")
    )
    incoming_lookup = {
        int(gid): list(zip(group["date"], group["sum_kzt"])) for gid, group in incoming_daily
    }
    outgoing_lookup = {
        int(gid): list(zip(group["date"], group["sum_kzt"])) for gid, group in outgoing_daily
    }

    in_days = tx.groupby("dst")["date"].nunique().to_dict()
    out_days = tx.groupby("src")["date"].nunique().to_dict()
    first_in = tx.groupby("dst")["date"].min().dt.day.to_dict()
    last_in = tx.groupby("dst")["date"].max().dt.day.to_dict()
    first_out = tx.groupby("src")["date"].min().dt.day.to_dict()
    last_out = tx.groupby("src")["date"].max().dt.day.to_dict()

    daily_in_sources = daily.groupby(["dst", "date"])["src"].nunique()
    daily_out_targets = daily.groupby(["src", "date"])["dst"].nunique()
    max_in_sources = daily_in_sources.groupby(level=0).max().to_dict()
    max_out_targets = daily_out_targets.groupby(level=0).max().to_dict()

    rows: list[dict[str, float | int]] = []
    for gid in (int(value) for value in nodes["gid"]):
        incoming = incoming_lookup.get(gid, [])
        outgoing = outgoing_lookup.get(gid, [])
        in_total = sum(float(amount) for _, amount in incoming)
        out_total = sum(float(amount) for _, amount in outgoing)
        denominator = min(in_total, out_total)
        matched_values: dict[int, float] = {}
        median_lag = float("nan")
        for lag in (0, 1, 2):
            matched, lag_median = _fifo_match(incoming, outgoing, lag)
            matched_values[lag] = matched / denominator if denominator > 0 else 0.0
            if lag == 2:
                median_lag = lag_median
        max_in = int(max_in_sources.get(gid, 0))
        max_out = int(max_out_targets.get(gid, 0))
        rows.append(
            {
                "gid": gid,
                "active_in_days": int(in_days.get(gid, 0)),
                "active_out_days": int(out_days.get(gid, 0)),
                "first_in_day": int(first_in.get(gid, 0)),
                "last_in_day": int(last_in.get(gid, 0)),
                "first_out_day": int(first_out.get(gid, 0)),
                "last_out_day": int(last_out.get(gid, 0)),
                "max_in_sources_day": max_in,
                "max_out_targets_day": max_out,
                "in_day_concentration": max_in / max(1, int(daily.loc[daily["dst"] == gid, "src"].nunique())),
                "out_day_concentration": max_out / max(1, int(daily.loc[daily["src"] == gid, "dst"].nunique())),
                "fifo_0d": float(matched_values[0]),
                "fifo_1d": float(matched_values[1]),
                "fifo_2d": float(matched_values[2]),
                "median_fifo_lag": median_lag,
            }
        )
    return pd.DataFrame(rows)


def repeated_route_features(nodes: pd.DataFrame, tx: pd.DataFrame) -> pd.DataFrame:
    """Count A→B→C routes recurring on at least two distinct day pairs."""

    daily = (
        tx.groupby(["src", "dst", "date"], as_index=False, sort=False)["sum_kzt"].sum()
    )
    inbound: dict[int, list[tuple[int, pd.Timestamp, float]]] = defaultdict(list)
    outbound_by_date: dict[int, dict[pd.Timestamp, list[tuple[int, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in daily.itertuples(index=False):
        inbound[int(row.dst)].append((int(row.src), row.date, float(row.sum_kzt)))
        outbound_by_date[int(row.src)][row.date].append((int(row.dst), float(row.sum_kzt)))

    output: list[dict[str, int | float]] = []
    for gid in (int(value) for value in nodes["gid"]):
        occurrences: dict[tuple[int, int], set[tuple[pd.Timestamp, pd.Timestamp]]] = defaultdict(set)
        volumes: dict[tuple[int, int], float] = defaultdict(float)
        for source, in_date, in_amount in inbound.get(gid, []):
            for lag in (0, 1):
                out_date = in_date + pd.Timedelta(days=lag)
                for target, out_amount in outbound_by_date.get(gid, {}).get(out_date, []):
                    if target == source:
                        continue
                    ratio = out_amount / max(in_amount, 1e-9)
                    if 0.5 <= ratio <= 2.0:
                        key = (source, target)
                        occurrences[key].add((in_date, out_date))
                        volumes[key] += min(in_amount, out_amount)
        repeated = [key for key, dates in occurrences.items() if len(dates) >= 2]
        output.append(
            {
                "gid": gid,
                "repeat_route_count": int(len(repeated)),
                "repeat_route_occurrences": int(sum(len(occurrences[key]) for key in repeated)),
                "repeat_route_kzt": float(sum(volumes[key] for key in repeated)),
            }
        )
    return pd.DataFrame(output)


def reciprocal_and_cycle_features(
    graph: nx.DiGraph,
    nodes: pd.DataFrame,
    tx: pd.DataFrame,
    max_cycle_length: int = 6,
) -> tuple[pd.DataFrame, dict[str, int]]:
    reciprocal: dict[int, int] = Counter()
    reciprocal_pairs = 0
    for source, target in graph.edges:
        if source < target and graph.has_edge(target, source):
            reciprocal[source] += 1
            reciprocal[target] += 1
            reciprocal_pairs += 1

    edge_dates: dict[tuple[int, int], list[pd.Timestamp]] = {
        (int(source), int(target)): sorted(group["date"].unique())
        for (source, target), group in tx.groupby(["src", "dst"])
    }

    def has_temporal_order(cycle: list[int]) -> bool:
        # A cycle has no intrinsic first edge OR first transaction. An older
        # unrelated transfer must not hide a later valid traversal. For a fixed
        # start, choosing the earliest feasible next date is complete: any later
        # choice can only shrink the remaining seven-day window.
        for start in range(len(cycle)):
            rotated = cycle[start:] + cycle[:start]
            route_dates = [edge_dates.get((source, rotated[(i + 1) % len(rotated)]), [])
                           for i, source in enumerate(rotated)]
            for first in route_dates[0]:
                chosen = first
                valid = True
                for dates in route_dates[1:]:
                    index = bisect_left(dates, chosen)
                    if index == len(dates) or (dates[index] - first).days > 7:
                        valid = False
                        break
                    chosen = dates[index]
                if valid:
                    return True
        return False

    cycle_count: dict[int, int] = Counter()
    temporal_cycle_count: dict[int, int] = Counter()
    n_cycles = 0
    n_temporal_cycles = 0
    for cycle in nx.simple_cycles(graph, length_bound=max_cycle_length):
        if len(cycle) < 2:
            continue
        n_cycles += 1
        temporal = has_temporal_order([int(node) for node in cycle])
        if temporal:
            n_temporal_cycles += 1
        for node in cycle:
            cycle_count[int(node)] += 1
            if temporal:
                temporal_cycle_count[int(node)] += 1

    rows = [
        {
            "gid": int(gid),
            "reciprocal_neighbors": int(reciprocal.get(int(gid), 0)),
            "cycle_count": int(cycle_count.get(int(gid), 0)),
            "temporal_cycle_count": int(temporal_cycle_count.get(int(gid), 0)),
        }
        for gid in nodes["gid"]
    ]
    return pd.DataFrame(rows), {
        "n_reciprocal_pairs": int(reciprocal_pairs),
        "n_cycles_length_2_6": int(n_cycles),
        "n_temporally_ordered_cycles_7d": int(n_temporal_cycles),
    }


def seed_features(graph: nx.DiGraph, nodes: pd.DataFrame) -> pd.DataFrame:
    seeds = [int(gid) for gid in nodes.loc[nodes["is_seed"], "gid"]]
    direct_sources: dict[int, set[int]] = defaultdict(set)
    for seed in seeds:
        direct_sources.update({target: direct_sources[target] | {seed} for target in graph.successors(seed)})

    affinities: dict[int, float] = defaultdict(float)
    reachable_seeds: dict[int, int] = defaultdict(int)
    min_depth: dict[int, int] = {}
    for seed in seeds:
        lengths = nx.single_source_shortest_path_length(graph, seed, cutoff=4)
        for node, distance in lengths.items():
            affinities[int(node)] += exp(-0.7 * int(distance))
            reachable_seeds[int(node)] += 1
            min_depth[int(node)] = min(min_depth.get(int(node), 99), int(distance))
    return pd.DataFrame(
        [
            {
                "gid": int(gid),
                "direct_seed_in": int(len(direct_sources.get(int(gid), set()))),
                "seed_affinity": float(affinities.get(int(gid), 0.0)),
                "n_seed_reachable": int(reachable_seeds.get(int(gid), 0)),
                "min_seed_distance": int(min_depth.get(int(gid), -1)),
            }
            for gid in nodes["gid"]
        ]
    )


def fragmentation_features(graph: nx.DiGraph, nodes: pd.DataFrame) -> pd.DataFrame:
    undirected = graph.to_undirected()
    articulation = set(nx.articulation_points(undirected))
    impact: dict[int, int] = defaultdict(int)
    for component_nodes in nx.connected_components(undirected):
        component_set = set(component_nodes)
        relevant = articulation & component_set
        if not relevant:
            continue
        subgraph = undirected.subgraph(component_set).copy()
        size = len(component_set)
        for node in relevant:
            reduced = nx.restricted_view(subgraph, [node], [])
            pieces = [len(piece) for piece in nx.connected_components(reduced)]
            impact[int(node)] = max(0, size - 1 - (max(pieces) if pieces else 0))
    return pd.DataFrame(
        [
            {
                "gid": int(gid),
                "is_articulation": int(int(gid) in articulation),
                "fragmentation_impact": int(impact.get(int(gid), 0)),
            }
            for gid in nodes["gid"]
        ]
    )


def assemble_base_features(
    graph: nx.DiGraph,
    nodes: pd.DataFrame,
    edges: pd.DataFrame,
    tx: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, int]]:
    frame = basic_and_centrality_features(graph, nodes)
    additions = [
        edge_distribution_features(edges),
        temporal_features(nodes, tx),
        repeated_route_features(nodes, tx),
        seed_features(graph, nodes),
        fragmentation_features(graph, nodes),
    ]
    cycles, motif_metadata = reciprocal_and_cycle_features(graph, nodes, tx)
    additions.append(cycles)
    for addition in additions:
        frame = frame.merge(addition, on="gid", how="left", validate="one_to_one")
    numeric_columns = frame.select_dtypes(include=[np.number]).columns
    frame[numeric_columns] = frame[numeric_columns].replace([np.inf, -np.inf], np.nan)
    return frame, motif_metadata
