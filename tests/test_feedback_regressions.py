from __future__ import annotations

import csv
from itertools import product
from pathlib import Path
import sys
import tempfile
import unittest

import networkx as nx
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from money_graph.features import reciprocal_and_cycle_features
from money_graph.contracts import CSV_COLUMNS
from money_graph.narration import role_evidence, priority_why
from money_graph.pipeline import PipelineConfig, run_pipeline
from money_graph.rules import PROFILE_TERMS, profile_formula, decision_trace


class FeedbackTests(unittest.TestCase):
    def cycle_result(self, dates):
        edges = [(1, 2), (2, 3), (3, 1)]
        graph = nx.DiGraph(edges)
        nodes = pd.DataFrame({"gid": [1, 2, 3]})
        tx = pd.DataFrame([(s, t, pd.Timestamp(f"2026-07-{day:02d}")) for (s, t), days in zip(edges, dates) for day in days], columns=["src", "dst", "date"])
        return reciprocal_and_cycle_features(graph, nodes, tx)

    def test_temporal_cycle_survives_older_unrelated_payment(self):
        features, metadata = self.cycle_result([[1, 20], [21], [22]])
        self.assertEqual(metadata["n_temporally_ordered_cycles_7d"], 1)
        self.assertTrue((features.temporal_cycle_count == 1).all())

    def test_temporal_cycle_matches_exhaustive_combinations(self):
        rng = np.random.default_rng(42)
        for _ in range(60):
            dates = [sorted(set(rng.integers(1, 32, size=4).tolist())) for _ in range(3)]
            expected = False
            for offset in range(3):
                rotated = dates[offset:] + dates[:offset]
                expected |= any(a <= b <= c and c - a <= 7 for a, b, c in product(*rotated))
            _, metadata = self.cycle_result(dates)
            self.assertEqual(metadata["n_temporally_ordered_cycles_7d"], int(expected), dates)

    def test_temporal_cycle_window_is_inclusive_and_counted_once(self):
        self.assertEqual(self.cycle_result([[1], [4], [8]])[1]["n_temporally_ordered_cycles_7d"], 1)
        self.assertEqual(self.cycle_result([[1], [4], [9]])[1]["n_temporally_ordered_cycles_7d"], 0)
        self.assertEqual(self.cycle_result([[1, 20], [1, 21], [1, 22]])[1]["n_temporally_ordered_cycles_7d"], 1)

    def test_fixed_csv_contract_and_extended_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            raw = base / "data"
            raw.mkdir()
            pd.DataFrame({"gid": [1, 2, 3], "depth": [0, 1, 4], "is_seed": [True, False, False]}).to_parquet(raw / "nodes.parquet")
            tx = pd.DataFrame({"src": [1, 2], "dst": [2, 3], "sum_kzt": [50000., 40000.], "date": ["2026-07-01", "2026-07-02"]})
            tx.to_parquet(raw / "transactions.parquet")
            tx.drop(columns="date").assign(n_tx=1, depth=[1, 4]).to_parquet(raw / "edges.parquet")
            metadata = run_pipeline(PipelineConfig(raw, base / "out", cluster_runs=2, role_stability_runs=2, validate=True))
            self.assertEqual(metadata["status"], "ok")
            for name, fields in CSV_COLUMNS.items():
                with (base / "out" / name).open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    self.assertEqual(tuple(reader.fieldnames), fields)
                    self.assertTrue(list(reader))
                extended = pd.read_csv(base / "out" / name.replace(".csv", "_extended.csv"))
                self.assertGreater(len(extended.columns), len(fields))
            from agent_app.store import GraphDataStore
            store = GraphDataStore(root=base, data_dir=raw, output_dir=base / "out")
            self.assertIn("fifo_1d", store.roles.columns)
            self.assertTrue((base / "out" / "decision_traces.json").is_file())

    def test_local_prose_preserves_censoring_and_balance_only_transit(self):
        row = dict(role="transit", in_deg=3, out_deg=2, in_kzt=100000, out_kzt=82130,
                   fifo_1d=.0453, depth=2, is_seed=False)
        self.assertIn("быстрое прохождение не подтверждено", role_evidence(row))
        for role in ("transit", "terminal", "consolidator", "distributor", "coordinator", "peripheral"):
            row.update(role=role, depth=4)
            for text in (role_evidence(row), priority_why(row)):
                self.assertLessEqual(len(text), 200)
                self.assertIn("исходящие потоки могут быть скрыты", text)
                self.assertNotIn("K=", text)

    def test_documented_profile_formulas_match_single_source(self):
        document = (ROOT / "docs" / "implemented_rules.md").read_text(encoding="utf-8")
        for role in PROFILE_TERMS:
            self.assertIn(profile_formula(role), document)

    def test_agent_and_scorer_share_same_gate_function(self):
        from agent_app.explanations import decision_trace as explained_trace
        self.assertIs(explained_trace, decision_trace)


if __name__ == "__main__":
    unittest.main()
