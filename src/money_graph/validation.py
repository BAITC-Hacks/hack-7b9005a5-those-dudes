from __future__ import annotations

import re

import networkx as nx
import numpy as np
import pandas as pd

from .clustering import adjusted_rand_index
from .features import _fifo_match, build_graph, reciprocal_and_cycle_features
from .scoring import ROLES
from .contracts import CSV_COLUMNS


def synthetic_motif_checks() -> dict[str, object]:
    """Small deterministic checks for the most important graph motifs."""

    day = pd.Timestamp("2026-07-10")
    matched_same, lag_same = _fifo_match([(day, 100.0)], [(day, 80.0)], 0)
    matched_next, lag_next = _fifo_match([(day, 100.0)], [(day + pd.Timedelta(days=1), 90.0)], 1)

    nodes = pd.DataFrame(
        {
            "gid": [1, 2, 3, 4, 5, 6],
            "depth": [0, 1, 1, 2, 2, 2],
            "is_seed": [True, False, False, False, False, False],
        }
    )
    # Fan-in at 4, fan-out at 4, reciprocal 2↔3 and directed cycle 2→3→4→2.
    edge_rows = [
        (1, 4, 100.0),
        (2, 4, 100.0),
        (3, 4, 100.0),
        (4, 5, 90.0),
        (4, 6, 90.0),
        (2, 3, 50.0),
        (3, 2, 50.0),
        (4, 2, 50.0),
    ]
    edges = pd.DataFrame(edge_rows, columns=["src", "dst", "sum_kzt"])
    edges["n_tx"] = 1
    edges["depth"] = 1
    transactions = edges[["src", "dst", "sum_kzt"]].copy()
    transactions["date"] = day
    graph = build_graph(nodes, edges)
    motifs, metadata = reciprocal_and_cycle_features(graph, nodes, transactions)
    motif_lookup = motifs.set_index("gid")

    checks = {
        "fifo_same_day_amount": bool(abs(matched_same - 80.0) < 1e-9 and lag_same == 0),
        "fifo_next_day_amount": bool(abs(matched_next - 90.0) < 1e-9 and lag_next == 1),
        "fan_in_detectable": bool(graph.in_degree(4) >= 3),
        "fan_out_detectable": bool(graph.out_degree(4) >= 2),
        "reciprocal_detectable": bool(motif_lookup.loc[2, "reciprocal_neighbors"] >= 1),
        "cycle_detectable": bool(metadata["n_cycles_length_2_6"] >= 1),
        "ari_label_invariant": bool(
            abs(adjusted_rand_index(np.array([0, 0, 1, 1]), np.array([7, 7, 3, 3])) - 1.0)
            < 1e-12
        ),
    }
    return {
        "passed": bool(all(checks.values())),
        "checks": checks,
        "note": "Synthetic checks validate detection mechanics and expected ordering, not real-world accuracy.",
    }


def validate_outputs(
    input_nodes: pd.DataFrame,
    nodes_roles: pd.DataFrame,
    clusters: pd.DataFrame,
    top_nodes: pd.DataFrame,
) -> dict[str, object]:
    numeric_evidence = nodes_roles["evidence"].astype(str).map(
        lambda value: bool(re.search(r"\d", value))
    )
    mandatory_node_columns = {
        "gid",
        "role",
        "role_score",
        "cluster_id",
        "priority_score",
        "evidence",
    }
    mandatory_cluster_columns = {
        "cluster_id",
        "n_nodes",
        "n_seed",
        "sum_kzt_internal",
        "top_gids",
        "hypothesis",
    }
    mandatory_top_columns = {"rank", "gid", "role", "priority_score", "why"}
    checks = {
        "fixed_csv_contract": all(tuple(frame.columns) == CSV_COLUMNS[name] for name, frame in
                                  (("nodes_roles.csv", nodes_roles), ("clusters.csv", clusters), ("top_nodes.csv", top_nodes))),
        "nodes_row_count_matches_input": len(nodes_roles) == len(input_nodes),
        "nodes_unique_gid": nodes_roles["gid"].astype(str).nunique() == len(input_nodes),
        "nodes_gid_set_matches_input": set(nodes_roles["gid"].astype(str))
        == set(input_nodes["gid"].astype(str)),
        "nodes_mandatory_columns": mandatory_node_columns.issubset(nodes_roles.columns),
        "roles_valid": set(nodes_roles["role"]).issubset(set(ROLES)),
        "role_scores_in_range": nodes_roles["role_score"].between(0.0, 1.0).all(),
        "priority_scores_in_range": nodes_roles["priority_score"].between(0.0, 1.0).all(),
        "evidence_nonempty_numeric_under_200": bool(
            nodes_roles["evidence"].astype(str).str.len().between(1, 200).all()
            and numeric_evidence.all()
        ),
        "clusters_mandatory_columns": mandatory_cluster_columns.issubset(clusters.columns),
        "all_nodes_have_cluster": int(clusters["n_nodes"].sum()) == len(input_nodes),
        "top_mandatory_columns": mandatory_top_columns.issubset(top_nodes.columns),
        "top_has_at_least_20_or_all_available_nodes": len(top_nodes) >= min(20, len(input_nodes)),
        "top_sorted_descending": top_nodes["priority_score"].is_monotonic_decreasing,
        "no_missing_mandatory_values": not nodes_roles[list(mandatory_node_columns)].isna().any().any(),
    }
    synthetic = synthetic_motif_checks()
    return {
        "passed": bool(all(checks.values()) and synthetic["passed"]),
        "schema_and_invariants": checks,
        "synthetic_motifs": synthetic,
        "interpretation": "Проверки измеряют корректность и устойчивость процедуры без ground truth; они не подтверждают роль или вину клиента.",
    }
