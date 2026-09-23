"""Strict upload validation and a real small-dataset pipeline regression."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from money_graph.io import load_inputs
from money_graph.pipeline import PipelineConfig, run_pipeline
from dashboard.build_dashboard import build as build_dashboard


def read_bundle(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")[len("window.HACKALEM_DATA="):-2])


def fixture() -> dict[str, pd.DataFrame]:
    return {
        "nodes": pd.DataFrame({"gid": [1, 2, 3], "depth": [0, 1, 2], "is_seed": [True, False, False]}),
        "edges": pd.DataFrame({"src": [1, 2], "dst": [2, 3], "sum_kzt": [10_000.0, 9_000.0], "n_tx": [1, 1], "depth": [1, 2]}),
        "transactions": pd.DataFrame({"src": [1, 2], "dst": [2, 3], "date": ["2026-08-01", "2026-08-02"], "sum_kzt": [10_000.0, 9_000.0]}),
    }


def write_fixture(path: Path, tables: dict[str, pd.DataFrame]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for name, frame in tables.items():
        frame.to_parquet(path / f"{name}.parquet", index=False)


class InputValidationTests(unittest.TestCase):
    def check_rejected(self, table: str, column: str, values: list, expected: str) -> None:
        tables = fixture()
        tables[table][column] = values
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(Path(tmp), tables)
            with self.assertRaisesRegex(ValueError, expected):
                load_inputs(Path(tmp))

    def test_original_inputs_still_load(self):
        result = load_inputs(ROOT / "input_data" / "data")
        self.assertEqual(result.checks["n_nodes"], 2248)
        self.assertTrue(result.checks["edge_transaction_reconciliation"])

    def test_rejects_nulls_in_every_required_column(self):
        for table, frame in fixture().items():
            for column in frame:
                with self.subTest(table=table, column=column):
                    values = frame[column].tolist()
                    values[0] = None
                    self.check_rejected(table, column, values, "пустые значения")

    def test_rejects_fractional_integer_fields(self):
        for table, column, values in (
            ("nodes", "gid", [1.5, 2.0, 3.0]),
            ("nodes", "depth", [0.0, 1.2, 2.0]),
            ("edges", "src", [1.1, 2.0]),
            ("edges", "dst", [2.0, 3.1]),
            ("edges", "depth", [1.2, 2.0]),
            ("edges", "n_tx", [1.5, 1.0]),
            ("transactions", "src", [1.1, 2.0]),
            ("transactions", "dst", [2.0, 3.1]),
        ):
            with self.subTest(table=table, column=column):
                self.check_rejected(table, column, values, "целые числа")

    def test_rejects_unsafe_float_and_out_of_range_ids(self):
        self.check_rejected("nodes", "gid", [float(2**53), 2.0, 3.0], "не float")
        self.check_rejected("nodes", "gid", [str(2**63), "2", "3"], "вне диапазона")
        self.check_rejected("nodes", "gid", [str(-(2**63) - 1), "2", "3"], "вне диапазона")
        self.check_rejected("nodes", "gid", [np.uint64(2**63), np.uint64(2), np.uint64(3)], "вне диапазона")

    def test_rejects_bad_depths_counts_and_booleans(self):
        self.check_rejected("nodes", "depth", [0, 1, 5], "вне диапазона")
        self.check_rejected("edges", "depth", [-1, 2], "вне диапазона")
        self.check_rejected("edges", "n_tx", [0, 1], "вне диапазона")
        self.check_rejected("nodes", "gid", [True, False, True], "не логическое")
        self.check_rejected("nodes", "is_seed", [1, 2, 0], "true/false")
        self.check_rejected("nodes", "is_seed", ["true", "unknown", "false"], "true/false")

    def test_false_string_remains_false(self):
        tables = fixture()
        tables["nodes"]["is_seed"] = ["true", "False", "0"]
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(Path(tmp), tables)
            self.assertEqual(load_inputs(Path(tmp)).nodes["is_seed"].tolist(), [True, False, False])

    def test_rejects_bad_amounts_and_dates(self):
        for table in ("edges", "transactions"):
            for value in (float("inf"), -float("inf"), -1.0, "bad"):
                with self.subTest(table=table, value=value):
                    values = [str(value), "9000"] if isinstance(value, str) else [value, 9000.0]
                    self.check_rejected(table, "sum_kzt", values, "конечными неотрицательными")
        self.check_rejected("transactions", "sum_kzt", [True, False], "не логическими")
        self.check_rejected("transactions", "date", ["not-a-date", "2026-08-02"], "корректные даты")
        self.check_rejected("transactions", "date", ["2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"], "без часового пояса")
        self.check_rejected("transactions", "date", [1, 2], "не числовые")

    def test_rejects_bad_references_and_reconciliation(self):
        self.check_rejected("edges", "dst", [2, 4], "отсутствующие в nodes")
        self.check_rejected("edges", "sum_kzt", [11_000, 9_000], "не сходится")
        self.check_rejected("edges", "n_tx", [2, 1], "не сходится")
        self.check_rejected("transactions", "dst", [3, 3], "не сходится")
        self.check_rejected("nodes", "gid", [1, 2, 2], "повторяющиеся gid")

    def test_small_network_pipeline_and_exact_int64_ids(self):
        tables = fixture()
        mapping = {1: 9_123_456_789_012_345_001, 2: 9_123_456_789_012_345_002, 3: 9_123_456_789_012_345_003}
        for frame in tables.values():
            for column in ("gid", "src", "dst"):
                if column in frame:
                    frame[column] = frame[column].map(mapping).astype("int64")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            write_fixture(path / "data", tables)
            metadata = run_pipeline(PipelineConfig(path / "data", path / "out", cluster_runs=2, role_stability_runs=2, validate=True))
            self.assertEqual(metadata["status"], "ok")
            for filename in ("nodes_roles.csv", "top_nodes.csv"):
                exported = pd.read_csv(path / "out" / filename, dtype={"gid": str})
                self.assertEqual(set(exported["gid"]), {str(value) for value in mapping.values()})
            self.assertEqual(metadata["input_checks"]["date_min"], "2026-08-01")
            bundle = read_bundle(build_dashboard(path / "data", path / "dist", path / "out"))
            self.assertEqual({node["gid"] for node in bundle["nodes"]}, {str(value) for value in mapping.values()})
            self.assertEqual({node["gid"] for node in bundle["production"]["top_nodes"]}, {str(value) for value in mapping.values()})

    def test_zero_amount_network_uses_explicit_count_fallback(self):
        tables = fixture()
        tables["edges"]["sum_kzt"] = 0.0
        tables["transactions"]["sum_kzt"] = 0.0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            write_fixture(path / "data", tables)
            metadata = run_pipeline(PipelineConfig(path / "data", path / "out", cluster_runs=2, role_stability_runs=2, validate=True))
            self.assertEqual(metadata["status"], "ok")
            self.assertEqual(metadata["cluster_metadata"]["primary_weight"], "count_weight")
            bundle = read_bundle(build_dashboard(path / "data", path / "dist", path / "out"))
            self.assertEqual(len(bundle["nodes"]), 3)

    def test_single_node_self_loop_pipeline_and_dashboard(self):
        tables = fixture()
        tables["nodes"] = tables["nodes"].iloc[:1].copy()
        tables["edges"] = tables["edges"].iloc[:1].copy()
        tables["transactions"] = tables["transactions"].iloc[:1].copy()
        tables["edges"]["dst"] = 1
        tables["transactions"]["dst"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            write_fixture(path / "data", tables)
            metadata = run_pipeline(PipelineConfig(path / "data", path / "out", cluster_runs=2, role_stability_runs=2, validate=True))
            self.assertEqual(metadata["status"], "ok")
            bundle = read_bundle(build_dashboard(path / "data", path / "dist", path / "out"))
            self.assertEqual(len(bundle["nodes"]), 1)
            self.assertEqual(bundle["nodes"][0]["gid"], "1")

    def test_dashboard_uses_same_normalized_values_as_pipeline(self):
        tables = fixture()
        tables["nodes"]["is_seed"] = ["true", "False", "0"]
        for frame in tables.values():
            for column in ("gid", "src", "dst", "sum_kzt", "n_tx", "depth"):
                if column in frame:
                    frame[column] = frame[column].astype(str)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            write_fixture(path / "data", tables)
            metadata = run_pipeline(PipelineConfig(path / "data", path / "out", cluster_runs=2, role_stability_runs=2, validate=True))
            self.assertEqual(metadata["status"], "ok")
            bundle = read_bundle(build_dashboard(path / "data", path / "dist", path / "out"))
            self.assertEqual([node["is_seed"] for node in bundle["nodes"]], [True, False, False])
            self.assertEqual(bundle["meta"]["n_nodes"], 3)


if __name__ == "__main__":
    unittest.main()
