from __future__ import annotations

from collections import defaultdict
from importlib.util import find_spec

import networkx as nx
import numpy as np
import pandas as pd


def _symmetrized_graph(graph: nx.DiGraph) -> nx.Graph:
    undirected = nx.Graph()
    undirected.add_nodes_from(graph.nodes)
    for source, target, data in graph.edges(data=True):
        # Preserve the supplied edge semantics: the symmetric pair weight is
        # the sum of both directed amounts/counts. Direction is retained in
        # all node features and explicitly reported as Louvain's trade-off.
        amount = float(data["sum_kzt"])
        count = float(data["n_tx"])
        if undirected.has_edge(source, target):
            undirected[source][target]["amount_weight"] += amount
            undirected[source][target]["count_weight"] += count
        else:
            undirected.add_edge(
                source,
                target,
                amount_weight=amount,
                count_weight=count,
            )
    return undirected


def _labels(communities: list[set[int]], nodes: list[int]) -> np.ndarray:
    lookup: dict[int, int] = {}
    for label, community in enumerate(communities):
        for node in community:
            lookup[int(node)] = label
    return np.asarray([lookup[int(node)] for node in nodes], dtype=int)


def adjusted_rand_index(left: np.ndarray, right: np.ndarray) -> float:
    """Dependency-free adjusted Rand index for two integer label arrays."""

    if len(left) != len(right):
        raise ValueError("Разбиения должны иметь одинаковую длину")
    n = len(left)
    if n < 2:
        return 1.0
    _, left_inverse = np.unique(left, return_inverse=True)
    _, right_inverse = np.unique(right, return_inverse=True)
    contingency = np.zeros(
        (int(left_inverse.max()) + 1, int(right_inverse.max()) + 1), dtype=np.int64
    )
    np.add.at(contingency, (left_inverse, right_inverse), 1)

    def choose_two(values: np.ndarray) -> float:
        values = values.astype(float)
        return float(np.sum(values * (values - 1.0) / 2.0))

    sum_cells = choose_two(contingency)
    sum_left = choose_two(contingency.sum(axis=1))
    sum_right = choose_two(contingency.sum(axis=0))
    total_pairs = n * (n - 1) / 2.0
    expected = (sum_left * sum_right / total_pairs) if total_pairs else 0.0
    maximum = 0.5 * (sum_left + sum_right)
    denominator = maximum - expected
    if denominator == 0:
        return 1.0
    return float((sum_cells - expected) / denominator)


def _stable_cluster_ids(communities: list[set[int]]) -> tuple[dict[int, int], list[set[int]]]:
    ordered = sorted(
        (set(int(node) for node in community) for community in communities),
        key=lambda community: (-len(community), min(community)),
    )
    lookup = {
        node: cluster_id
        for cluster_id, community in enumerate(ordered)
        for node in community
    }
    return lookup, ordered


def louvain_ensemble(
    graph: nx.DiGraph,
    n_runs: int = 16,
    random_seed: int = 42,
) -> tuple[dict[int, int], pd.DataFrame, dict[str, object], list[set[int]]]:
    """Select the medoid of repeated amount-weighted Louvain partitions.

    Direction is retained in all upstream features. Louvain is explicitly a
    symmetrized baseline; its amount and count variants are compared rather
    than presented as a directed ground truth.
    """

    undirected = _symmetrized_graph(graph)
    nodes = sorted(int(node) for node in graph.nodes)
    if undirected.number_of_edges() == 0:
        communities = [{node} for node in nodes]
        lookup, ordered = _stable_cluster_ids(communities)
        stability = pd.DataFrame(
            {"gid": nodes, "cluster_id": [lookup[node] for node in nodes], "cluster_stability": 1.0}
        )
        return lookup, stability, {"method": "singleton", "n_runs": 1}, ordered

    partitions: list[list[set[int]]] = []
    label_arrays: list[np.ndarray] = []
    # Zero-value transactions remain valid observations. Amount-weighted
    # modularity is undefined if every weight is zero, so use observed counts.
    primary_weight = "amount_weight" if undirected.size(weight="amount_weight") > 0 else "count_weight"
    for run in range(n_runs):
        communities = [
            set(int(node) for node in community)
            for community in nx.community.louvain_communities(
                undirected,
                weight=primary_weight,
                resolution=1.0,
                seed=random_seed + run,
            )
        ]
        partitions.append(communities)
        label_arrays.append(_labels(communities, nodes))

    ari_matrix = np.eye(n_runs, dtype=float)
    for left in range(n_runs):
        for right in range(left + 1, n_runs):
            score = adjusted_rand_index(label_arrays[left], label_arrays[right])
            ari_matrix[left, right] = score
            ari_matrix[right, left] = score
    medoid_index = int(np.argmax(ari_matrix.mean(axis=1)))
    medoid = partitions[medoid_index]
    lookup, ordered = _stable_cluster_ids(medoid)

    medoid_communities = {node: community for community in ordered for node in community}
    per_run_communities: list[dict[int, set[int]]] = []
    for partition in partitions:
        per_run_communities.append({node: community for community in partition for node in community})
    stability_values: dict[int, float] = {}
    for node in nodes:
        reference = medoid_communities[node]
        similarities = []
        for run_lookup in per_run_communities:
            candidate = run_lookup[node]
            similarities.append(len(reference & candidate) / max(1, len(reference | candidate)))
        stability_values[node] = float(np.mean(similarities))

    count_communities = [
        set(int(node) for node in community)
        for community in nx.community.louvain_communities(
            undirected,
            weight="count_weight",
            resolution=1.0,
            seed=random_seed,
        )
    ]
    count_ari = adjusted_rand_index(
        _labels(medoid, nodes),
        _labels(count_communities, nodes),
    )
    upper = ari_matrix[np.triu_indices(n_runs, k=1)]
    metadata: dict[str, object] = {
        "method": "louvain_symmetrized_amount_medoid" if primary_weight == "amount_weight" else "louvain_symmetrized_count_medoid_zero_amount_fallback",
        "primary_weight": primary_weight,
        "direction_tradeoff": "Louvain использует симметризованную проекцию; направленные признаки сохранены отдельно",
        "n_runs": int(n_runs),
        "random_seed": int(random_seed),
        "medoid_run": int(medoid_index),
        "n_clusters": int(len(ordered)),
        "pairwise_ari_median": float(np.median(upper)) if len(upper) else 1.0,
        "pairwise_ari_min": float(np.min(upper)) if len(upper) else 1.0,
        "amount_vs_count_ari": float(count_ari),
        "optional_infomap_available": bool(find_spec("infomap")),
        "optional_leiden_available": bool(find_spec("leidenalg") and find_spec("igraph")),
        "optional_sbm_available": bool(find_spec("graph_tool")),
        "challenger_note": (
            "Infomap/Leiden/SBM не блокируют локальный baseline; при установке их следует "
            "сравнивать по ARI/VI, а не смешивать границы автоматически"
        ),
    }
    stability = pd.DataFrame(
        {
            "gid": nodes,
            "cluster_id": [lookup[node] for node in nodes],
            "cluster_stability": [stability_values[node] for node in nodes],
        }
    )
    return lookup, stability, metadata, ordered


def add_cluster_graph_features(
    graph: nx.DiGraph,
    features: pd.DataFrame,
    cluster_lookup: dict[int, int],
) -> pd.DataFrame:
    frame = features.copy()
    frame["cluster_id"] = frame["gid"].map(cluster_lookup).astype(int)
    participation: dict[int, float] = {}
    clusters_touched: dict[int, int] = {}
    boundary_kzt: dict[int, float] = {}
    for node in graph.nodes:
        per_cluster: dict[int, float] = defaultdict(float)
        total = 0.0
        external = 0.0
        for _, target, data in graph.out_edges(node, data=True):
            weight = float(data["sum_kzt"])
            per_cluster[cluster_lookup[int(target)]] += weight
            total += weight
            if cluster_lookup[int(target)] != cluster_lookup[int(node)]:
                external += weight
        for source, _, data in graph.in_edges(node, data=True):
            weight = float(data["sum_kzt"])
            per_cluster[cluster_lookup[int(source)]] += weight
            total += weight
            if cluster_lookup[int(source)] != cluster_lookup[int(node)]:
                external += weight
        participation[int(node)] = (
            1.0 - sum((value / total) ** 2 for value in per_cluster.values()) if total > 0 else 0.0
        )
        clusters_touched[int(node)] = len(per_cluster)
        boundary_kzt[int(node)] = external
    frame["participation"] = frame["gid"].map(participation).fillna(0.0)
    frame["clusters_touched"] = frame["gid"].map(clusters_touched).fillna(0).astype(int)
    frame["boundary_kzt"] = frame["gid"].map(boundary_kzt).fillna(0.0)

    weak_components = sorted(
        (set(component) for component in nx.weakly_connected_components(graph)),
        key=lambda component: (-len(component), min(component)),
    )
    component_lookup = {
        int(node): component_id
        for component_id, component in enumerate(weak_components)
        for node in component
    }
    frame["component_id"] = frame["gid"].map(component_lookup).astype(int)
    return frame
