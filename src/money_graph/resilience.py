from __future__ import annotations

import random

import networkx as nx
import pandas as pd


def _reachable_from_seeds(graph: nx.DiGraph, seeds: set[int]) -> int:
    active_seeds = seeds & set(graph.nodes)
    reached = set(active_seeds)
    frontier = list(active_seeds)
    while frontier:
        source = frontier.pop()
        for target in graph.successors(source):
            if target not in reached:
                reached.add(target)
                frontier.append(target)
    return len(reached)


def _metrics(
    graph: nx.DiGraph,
    original_lcc: int,
    original_kzt: float,
    original_reachable: int,
    seeds: set[int],
) -> dict[str, float | int]:
    components = list(nx.weakly_connected_components(graph))
    lcc_nodes = max((len(component) for component in components), default=0)
    remaining_kzt = sum(float(data["sum_kzt"]) for _, _, data in graph.edges(data=True))
    reachable = _reachable_from_seeds(graph, seeds)
    return {
        "lcc_nodes": int(lcc_nodes),
        "lcc_ratio": float(lcc_nodes / max(1, original_lcc)),
        "n_components": int(len(components)),
        "remaining_edge_weight_ratio": float(remaining_kzt / max(original_kzt, 1e-9)),
        "reachable_seed_ratio": float(reachable / max(1, original_reachable)),
    }


def resilience_analysis(
    graph: nx.DiGraph,
    features: pd.DataFrame,
    n_values: tuple[int, ...] = (0, 1, 5, 10, 20, 50),
    random_seed: int = 42,
    random_runs: int = 30,
) -> pd.DataFrame:
    original_components = list(nx.weakly_connected_components(graph))
    original_lcc = max((len(component) for component in original_components), default=0)
    original_kzt = sum(float(data["sum_kzt"]) for _, _, data in graph.edges(data=True))
    seeds = set(int(value) for value in features.loc[features["is_seed"], "gid"])
    original_reachable = _reachable_from_seeds(graph, seeds)
    strategies = {
        "priority": features.sort_values(
            ["priority_score", "gid"], ascending=[False, True]
        )["gid"].astype(int).tolist(),
        "betweenness": features.sort_values(
            ["betweenness", "gid"], ascending=[False, True]
        )["gid"].astype(int).tolist(),
        "pagerank_amount": features.sort_values(
            ["pagerank_amount", "gid"], ascending=[False, True]
        )["gid"].astype(int).tolist(),
    }
    rows: list[dict[str, object]] = []
    for strategy, ordering in strategies.items():
        for raw_n in n_values:
            n_removed = min(int(raw_n), graph.number_of_nodes())
            reduced = graph.copy()
            reduced.remove_nodes_from(ordering[:n_removed])
            rows.append(
                {
                    "strategy": strategy,
                    "n_removed": n_removed,
                    **_metrics(
                        reduced,
                        original_lcc,
                        original_kzt,
                        original_reachable,
                        seeds,
                    ),
                }
            )

    rng = random.Random(random_seed)
    all_nodes = list(graph.nodes)
    for raw_n in n_values:
        n_removed = min(int(raw_n), len(all_nodes))
        simulations: list[dict[str, float | int]] = []
        for _ in range(random_runs):
            removed = rng.sample(all_nodes, n_removed) if n_removed else []
            reduced = graph.copy()
            reduced.remove_nodes_from(removed)
            simulations.append(
                _metrics(
                    reduced,
                    original_lcc,
                    original_kzt,
                    original_reachable,
                    seeds,
                )
            )
        rows.append(
            {
                "strategy": "random_baseline_mean",
                "n_removed": n_removed,
                **{
                    key: float(sum(float(result[key]) for result in simulations) / len(simulations))
                    for key in simulations[0]
                },
            }
        )
    return pd.DataFrame(rows)
