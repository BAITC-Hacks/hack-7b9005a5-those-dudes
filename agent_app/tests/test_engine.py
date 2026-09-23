from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from agent_app.agent import run_sdk_query
from agent_app.engine import ANALYST_CAVEAT, InvestigationEngine
from agent_app.store import GraphDataStore


G1 = "100000000000000001"
G2 = "100000000000000002"
G3 = "100000000000000003"
G4 = "100000000000000004"


class EngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        data = root / "input_data" / "data"
        output = root / "output"
        data.mkdir(parents=True)
        output.mkdir()

        pd.DataFrame(
            [
                {"gid": G1, "depth": 0, "is_seed": True},
                {"gid": G2, "depth": 1, "is_seed": False},
                {"gid": G3, "depth": 2, "is_seed": False},
                {"gid": G4, "depth": 4, "is_seed": False},
            ]
        ).to_parquet(data / "nodes.parquet", index=False)
        pd.DataFrame(
            [
                {"src": G1, "dst": G2, "sum_kzt": 100_000.0, "n_tx": 2, "depth": 1},
                {"src": G1, "dst": G3, "sum_kzt": 30_000.0, "n_tx": 1, "depth": 1},
                {"src": G2, "dst": G3, "sum_kzt": 80_000.0, "n_tx": 1, "depth": 2},
                {"src": G2, "dst": G4, "sum_kzt": 10_000.0, "n_tx": 1, "depth": 4},
            ]
        ).to_parquet(data / "edges.parquet", index=False)
        pd.DataFrame(
            [
                {"src": G1, "dst": G2, "sum_kzt": 50_000.0, "date": "2026-07-01"},
                {"src": G1, "dst": G2, "sum_kzt": 50_000.0, "date": "2026-07-01"},
                {"src": G1, "dst": G3, "sum_kzt": 30_000.0, "date": "2026-07-01"},
                {"src": G2, "dst": G3, "sum_kzt": 80_000.0, "date": "2026-07-02"},
                {"src": G2, "dst": G4, "sum_kzt": 10_000.0, "date": "2026-07-02"},
            ]
        ).to_parquet(data / "transactions.parquet", index=False)
        pd.DataFrame(
            [
                {
                    "gid": G1,
                    "role": "distributor",
                    "role_score": 0.88,
                    "cluster_id": 0,
                    "priority_score": 0.92,
                    "evidence": "out_deg=2; out_kzt=130000",
                    "depth": 0,
                    "is_seed": True,
                    "truncated_by_depth": False,
                    "in_deg": 0,
                    "out_deg": 2,
                    "in_kzt": 0,
                    "out_kzt": 130000,
                    "priority_connectivity": 0.9,
                    "priority_seed": 1.0,
                },
                {
                    "gid": G2,
                    "role": "transit",
                    "role_score": 0.84,
                    "cluster_id": 0,
                    "priority_score": 0.81,
                    "evidence": "fifo_1d=0.9; pass=0.9",
                    "depth": 1,
                    "is_seed": False,
                    "truncated_by_depth": False,
                    "in_deg": 1,
                    "out_deg": 2,
                    "in_kzt": 100000,
                    "out_kzt": 90000,
                    "fifo_1d": 0.9,
                },
                {
                    "gid": G3,
                    "role": "consolidator",
                    "role_score": 0.76,
                    "cluster_id": 0,
                    "priority_score": 0.73,
                    "evidence": "in_deg=2; in_kzt=110000",
                    "depth": 2,
                    "is_seed": False,
                    "truncated_by_depth": False,
                    "in_deg": 2,
                    "out_deg": 0,
                    "in_kzt": 110000,
                    "out_kzt": 0,
                },
                {
                    "gid": G4,
                    "role": "peripheral",
                    "role_score": 0.42,
                    "cluster_id": 0,
                    "priority_score": 0.55,
                    "evidence": "depth=4; p_continue=0.61",
                    "uncertainty_reason": "depth-4 censoring",
                    "depth": 4,
                    "is_seed": False,
                    "truncated_by_depth": True,
                    "p_continue": 0.61,
                    "in_deg": 1,
                    "out_deg": 0,
                    "in_kzt": 10000,
                    "out_kzt": 0,
                },
            ]
        ).to_csv(output / "nodes_roles.csv", index=False)
        pd.DataFrame(
            [
                {
                    "cluster_id": 0,
                    "n_nodes": 4,
                    "n_seed": 1,
                    "sum_kzt_internal": 220000,
                    "top_gids": f"{G1}|{G2}",
                    "hypothesis": "fan-out / transit motif",
                }
            ]
        ).to_csv(output / "clusters.csv", index=False)
        pd.DataFrame(
            [{"rank": 1, "gid": G1, "role": "distributor", "priority_score": 0.92, "why": "out_deg=2"}]
        ).to_csv(output / "top_nodes.csv", index=False)
        pd.DataFrame(
            [
                {
                    "strategy": "priority",
                    "n_removed": 1,
                    "lcc_nodes": 2,
                    "lcc_ratio": 0.5,
                    "n_components": 2,
                    "remaining_edge_weight_ratio": 0.4,
                    "reachable_seed_ratio": 0.5,
                }
            ]
        ).to_csv(output / "resilience.csv", index=False)

        self.store = GraphDataStore(root=root, strict_outputs=True)
        self.engine = InvestigationEngine(self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_identifier_is_never_rounded(self) -> None:
        result = self.engine.node_profile(G1)
        self.assertEqual(result["data"]["gid"], G1)
        self.assertIn(G1, json.dumps(result))

    def test_depth4_profile_has_censoring_warning(self) -> None:
        result = self.engine.node_profile(G4)
        self.assertTrue(result["data"]["sampling"]["truncated_by_depth"])
        self.assertTrue(any("правоцензурирован" in warning for warning in result["warnings"]))
        self.assertEqual(result["caveat"], ANALYST_CAVEAT)

    def test_neighbors_and_routes_are_directed(self) -> None:
        outgoing = self.engine.neighbors(G2, "out", 10)
        self.assertEqual({item["gid"] for item in outgoing["data"]["neighbors"]}, {G3, G4})
        routes = self.engine.trace_routes(G1, max_hops=2, limit=10)
        paths = [item["path"] for item in routes["data"]["routes"]]
        self.assertIn([G1, G2, G3], paths)

    def test_common_recipients(self) -> None:
        result = self.engine.common_recipients([G1, G2])
        self.assertEqual(result["data"]["recipients"][0]["gid"], G3)
        self.assertEqual(result["data"]["recipients"][0]["n_sources"], 2)

    def test_compare_nodes_preserves_role_and_priority(self) -> None:
        result = self.engine.compare_nodes([G1, G2])
        self.assertEqual(result["status"], "ok")
        self.assertEqual([item["gid"] for item in result["data"]["nodes"]], [G1, G2])
        self.assertEqual(result["data"]["nodes"][0]["role"], "distributor")

    def test_priority_explanation_is_numeric(self) -> None:
        result = self.engine.explain_priority(G1)
        self.assertEqual(result["data"]["priority_score"], 0.92)
        self.assertEqual(result["data"]["components"]["connectivity"], 0.9)

    def test_offline_router(self) -> None:
        result = self.engine.answer_offline(f"объясни приоритет {G2}")
        self.assertEqual(result["action"], "explain_priority")
        self.assertEqual(result["data"]["gid"], G2)

    def test_unknown_gid_is_explicit(self) -> None:
        result = self.engine.node_profile("999999999999999999")
        self.assertEqual(result["status"], "not_found")

    def test_sdk_mode_stops_before_api_without_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                asyncio.run(run_sdk_query("test", store=self.store))


if __name__ == "__main__":
    unittest.main()
