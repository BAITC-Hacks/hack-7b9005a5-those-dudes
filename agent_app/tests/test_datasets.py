from __future__ import annotations

from io import BytesIO
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import pyarrow as pa
import pyarrow.parquet as pq

from agent_app.datasets import DatasetError, DatasetManager, EXPORT_NAMES, _preflight


def parquet_bytes(values: dict) -> bytes:
    sink = pa.BufferOutputStream()
    pq.write_table(pa.table(values), sink)
    return sink.getvalue().to_pybytes()


def valid_files() -> dict[str, bytes]:
    return {
        "nodes": parquet_bytes({"gid": [1, 2], "depth": [0, 1], "is_seed": [True, False]}),
        "edges": parquet_bytes({"src": [1], "dst": [2], "sum_kzt": [5000.0], "n_tx": [1], "depth": [1]}),
        "transactions": parquet_bytes({"src": [1], "dst": [2], "date": ["2026-07-01"], "sum_kzt": [5000.0]}),
    }


class PreflightTests(unittest.TestCase):
    def test_valid_tables_counted_without_loading_dataframes(self):
        self.assertEqual(_preflight(valid_files()), {"nodes": 2, "edges": 1, "transactions": 1})

    def test_requires_exactly_three_nonempty_parquets(self):
        for files in ({}, {**valid_files(), "extra": b"hi"}, {**valid_files(), "nodes": b""},
                      {**valid_files(), "nodes": b"not-a-parquet"}):
            with self.subTest(keys=files.keys()), self.assertRaises(DatasetError):
                _preflight(files)

    def test_missing_columns_is_actionable(self):
        with self.assertRaisesRegex(DatasetError, "is_seed"):
            _preflight({**valid_files(), "nodes": parquet_bytes({"gid": [1], "depth": [0]})})

    def test_empty_tables_rejected(self):
        with self.assertRaisesRegex(DatasetError, "от 1"):
            _preflight({**valid_files(), "nodes": parquet_bytes({"gid": [], "depth": [], "is_seed": []})})

    def test_limits_rows_and_compressed_size(self):
        with patch("agent_app.datasets.ROW_LIMITS", {"nodes": 1, "edges": 1, "transactions": 1}):
            with self.assertRaises(DatasetError):
                _preflight(valid_files())
        with patch("agent_app.datasets.MAX_FILE_BYTES", 10):
            with self.assertRaises(DatasetError) as caught:
                _preflight(valid_files())
            self.assertEqual(caught.exception.status, 413)
        with patch("agent_app.datasets.MAX_UNCOMPRESSED_BYTES", 1):
            with self.assertRaises(DatasetError) as caught:
                _preflight(valid_files())
            self.assertEqual(caught.exception.status, 413)

    def test_oversized_metadata_footer_is_rejected(self):
        payload = b"PAR1" + b"data" + (5 * 1024 * 1024).to_bytes(4, "little") + b"PAR1"
        with self.assertRaisesRegex(DatasetError, "метаданные"):
            _preflight({**valid_files(), "nodes": payload})


class DatasetManagerTests(unittest.TestCase):
    def test_old_results_have_explicit_non_mutating_recalculation_notice(self):
        import json
        self.assertIn("предыдущей версией", self.manager.current()["calculation_notice"])
        (self.output / "thresholds.json").write_text(json.dumps({"rules_source": "money_graph.rules.decision_trace"}), encoding="utf-8")
        self.assertEqual(self.manager.current()["calculation_notice"], "")

    def test_competition_zip_is_fixed_and_keeps_ids_without_api(self):
        import csv
        from io import StringIO
        from money_graph.contracts import CSV_COLUMNS
        for name, fields in CSV_COLUMNS.items():
            row = {field: "1" for field in fields}
            if "gid" in row:
                row["gid"] = "100000000000000001"
            for field in ("evidence", "why", "hypothesis"):
                if field in row:
                    row[field] = "Глубина 4: выход может быть скрыт."
            with (self.output / name).open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=[*fields, "extra"])
                writer.writeheader()
                writer.writerow({**row, "extra": "original extra"})
        with ZipFile(BytesIO(self.manager.competition_archive("default"))) as archive:
            self.assertEqual(set(archive.namelist()), set(CSV_COLUMNS))
            for name, fields in CSV_COLUMNS.items():
                reader = csv.DictReader(StringIO(archive.read(name).decode("utf-8-sig")))
                self.assertEqual(tuple(reader.fieldnames), fields)
                rows = list(reader)
                if "gid" in fields:
                    self.assertEqual(rows[0]["gid"], "100000000000000001")
        self.assertIn(b"extra", (self.output / "nodes_roles.csv").read_bytes())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.static = self.root / "dashboard" / "dist"
        self.static.mkdir(parents=True)
        (self.static / "dashboard_data.js").write_text("window.HACKALEM_DATA={};", encoding="utf-8")
        self.output = self.root / "output"
        self.output.mkdir()
        for name in EXPORT_NAMES:
            (self.output / name).write_bytes(b"example,original\n")
        store = SimpleNamespace(nodes=[1, 2], edges=[1], transactions=[1], pipeline_ready=True,
                                paths=SimpleNamespace(output_dir=self.output))
        self.engine = SimpleNamespace(store=store)
        self.explanations = object()
        self.manager = DatasetManager(self.root, self.engine, self.explanations, self.static)

    def submit_paused(self):
        with patch("agent_app.datasets.Thread.start"):
            return self.manager.submit(valid_files())["run_id"]

    def complete_outputs(self, _command, directory, _deadline):
        (directory / "output").mkdir(exist_ok=True)
        (directory / "public").mkdir(exist_ok=True)
        for name in EXPORT_NAMES:
            (directory / "output" / name).write_bytes(b"uploaded,new\n")
        (directory / "public" / "dashboard_data.js").write_text("window.HACKALEM_DATA={};", encoding="utf-8")

    def finish(self, run_id):
        with patch.object(self.manager, "_run_command", side_effect=self.complete_outputs), \
             patch.object(self.manager, "_load_context", return_value=("new-engine", "new-explanations")):
            self.manager._process(run_id)

    def test_default_is_available_and_exact_zip_exports(self):
        self.assertEqual(self.manager.current()["status"], "ready")
        self.assertEqual(self.manager.context("default"), (self.engine, self.explanations))
        with ZipFile(BytesIO(self.manager.archive("default"))) as archive:
            self.assertEqual(archive.namelist(), list(EXPORT_NAMES))
            self.assertEqual(archive.read("nodes_roles.csv"), (self.output / "nodes_roles.csv").read_bytes())

    def test_upload_is_immutable_until_ready_and_default_unchanged(self):
        run_id = self.submit_paused()
        self.assertEqual(self.manager.status(run_id)["status"], "queued")
        with self.assertRaises(DatasetError) as caught:
            self.manager.asset(run_id, "nodes_roles.csv")
        self.assertEqual(caught.exception.status, 409)
        self.finish(run_id)
        self.assertEqual(self.manager.status(run_id)["status"], "ready")
        self.assertEqual(self.manager.context(run_id), ("new-engine", "new-explanations"))
        self.assertEqual(self.manager.asset(run_id, "nodes_roles.csv").read_bytes(), b"uploaded,new\n")
        self.assertEqual(self.manager.asset("default", "nodes_roles.csv").read_bytes(), b"example,original\n")

    def test_only_one_upload_analysis_at_once(self):
        first = self.submit_paused()
        with self.assertRaises(DatasetError) as caught:
            self.submit_paused()
        self.assertEqual(caught.exception.status, 409)
        self.finish(first)
        second = self.submit_paused()
        self.assertNotEqual(first, second)
        self.assertEqual(self.manager.status(first)["status"], "ready")

    def test_ready_survives_restart_and_interrupted_is_not_ready(self):
        complete = self.submit_paused()
        self.finish(complete)
        interrupted = self.submit_paused()
        restored = DatasetManager(self.root, self.engine, self.explanations, self.static)
        self.assertEqual(restored.status(complete)["status"], "ready")
        self.assertEqual(restored.status(interrupted)["status"], "failed")
        self.assertEqual(restored.asset(complete, "clusters.csv").read_bytes(), b"uploaded,new\n")

    def test_timeout_and_failure_never_publish_partial_outputs(self):
        for error in (subprocess.TimeoutExpired("test", 300), DatasetError("Bad input"), RuntimeError("private detail")):
            run_id = self.submit_paused()
            with patch.object(self.manager, "_run_command", side_effect=error):
                self.manager._process(run_id)
            state = self.manager.status(run_id)
            self.assertEqual(state["status"], "failed")
            self.assertNotIn("private detail", state["error"])
            self.assertIsNone(self.manager._active)
            with self.assertRaises(DatasetError):
                self.manager.archive(run_id)

    def test_export_filename_and_run_allowlists_block_traversal(self):
        for filename in ("../.env", ".env", "run_metadata.json", "../output/nodes_roles.csv"):
            with self.subTest(filename=filename), self.assertRaises(DatasetError) as caught:
                self.manager.asset("default", filename)
            self.assertEqual(caught.exception.status, 404)
        for run_id in ("../output", "", "a" * 31, "A" * 32):
            with self.subTest(run_id=run_id), self.assertRaises(DatasetError) as caught:
                self.manager.status(run_id)
            self.assertEqual(caught.exception.status, 404)

    def test_missing_ready_file_after_restart_blocks_export(self):
        run_id = self.submit_paused()
        self.finish(run_id)
        self.manager.asset(run_id, "top_nodes.csv").unlink()
        restored = DatasetManager(self.root, self.engine, self.explanations, self.static)
        self.assertEqual(restored.status(run_id)["status"], "failed")

    def test_subprocess_environment_does_not_need_ai_credentials(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "not-a-real-test-key"}):
            self.assertNotIn("OPENAI_API_KEY", self.manager._environment())

    def test_known_validation_error_is_shown_without_traceback(self):
        import time
        run_id = self.submit_paused()
        directory = self.manager._directory(run_id)
        def failure(_command, **kwargs):
            kwargs["stdout"].write(b"Traceback private path\nValueError: edges.parquet does not match transactions\n")
            return SimpleNamespace(returncode=1)
        with patch("agent_app.datasets.subprocess.run", side_effect=failure):
            with self.assertRaises(DatasetError) as caught:
                self.manager._run_command(["not-run"], directory, time.monotonic() + 30)
        self.assertIn("edges.parquet", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
