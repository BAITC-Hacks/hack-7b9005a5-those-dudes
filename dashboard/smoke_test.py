#!/usr/bin/env python3
"""Small end-to-end validation for the local dashboard bundle."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd

from build_dashboard import DEFAULT_DATA, OPTIONAL_OUTPUT, build


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        output = build(DEFAULT_DATA, Path(directory), OPTIONAL_OUTPUT)
        source = output.read_text(encoding="utf-8")
        assert source.startswith("window.HACKALEM_DATA=") and source.endswith(";\n")
        payload = json.loads(source[len("window.HACKALEM_DATA=") : -2])

        assert payload["meta"]["n_nodes"] == 2_248
        assert payload["meta"]["n_edges"] == 3_119
        assert payload["meta"]["n_transactions"] == 4_840
        assert payload["meta"]["reconciliation_ok"] is True
        assert len(payload["nodes"]) == 2_248
        assert len(payload["edges"]) == 3_119
        assert len(payload["transactions"]) == 4_840
        assert all(isinstance(row["gid"], str) for row in payload["nodes"])
        assert all(isinstance(row["src"], str) and isinstance(row["dst"], str) for row in payload["edges"])
        assert sum(bool(row["truncated_by_depth"]) for row in payload["nodes"]) == 444
        assert sum(bool(row["isolated"]) for row in payload["nodes"]) == 19
        assert payload["meta"]["n_fifo_0d_80"] == 139
        assert payload["meta"]["n_fifo_1d_80"] == 206
        assert payload["meta"]["n_reciprocal_pairs"] == 177
        assert payload["meta"]["n_multi_seed_direct"] == 24

    # Production artifacts are optional, schema-tolerant, and must preserve ids.
    first_gid = str(pd.read_parquet(DEFAULT_DATA / "nodes.parquet").iloc[0].gid)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        outputs = root / "outputs"
        outputs.mkdir()
        pd.DataFrame(
            [{
                "gid": first_gid,
                "role": "coordinator",
                "role_score": 0.72,
                "secondary_role": "consolidator",
                "uncertainty_reason": "scores close",
                "p_continue": 0.33,
                "cluster_id": 700,
                "priority_score": 0.97,
                "evidence": "betweenness p=0.98",
            }]
        ).to_csv(outputs / "nodes_roles.csv", index=False)
        pd.DataFrame(
            [{"cluster_id": 700, "n_nodes": 1, "n_seed": 1, "hypothesis": "test hypothesis"}]
        ).to_csv(outputs / "clusters.csv", index=False)
        pd.DataFrame(
            [{"rank": 1, "gid": first_gid, "role": "coordinator", "priority_score": 0.97, "why": "test"}]
        ).to_csv(outputs / "top_nodes.csv", index=False)
        pd.DataFrame(
            [{"strategy": "brokerage", "n_removed": 1, "lcc_nodes": 1800, "lcc_ratio": 0.96, "n_components": 36}]
        ).to_csv(outputs / "resilience.csv", index=False)
        pd.DataFrame([{"gid": first_gid, "extra_metric": 0.5}]).to_csv(
            outputs / "features.csv", index=False
        )
        (outputs / "thresholds.json").write_text('{"in_degree_q95": 3}', encoding="utf-8")
        (outputs / "run_metadata.json").write_text('{"cluster_method": "Infomap"}', encoding="utf-8")
        (outputs / "validation_report.json").write_text('{"role_agreement": 0.91}', encoding="utf-8")

        output = build(DEFAULT_DATA, root / "dist", outputs)
        source = output.read_text(encoding="utf-8")
        payload = json.loads(source[len("window.HACKALEM_DATA=") : -2])
        node = next(row for row in payload["nodes"] if row["gid"] == first_gid)
        assert node["role"] == "coordinator"
        assert node["cluster_id"] == 700
        assert node["secondary_role"] == "consolidator"
        assert payload["meta"]["has_roles"] is True
        assert payload["meta"]["has_resilience"] is True
        assert payload["production"]["top_nodes"][0]["gid"] == first_gid
        assert payload["production"]["features"][0]["gid"] == first_gid
        assert payload["production"]["validation"]["role_agreement"] == 0.91

    print("Dashboard smoke test: OK")


if __name__ == "__main__":
    main()
