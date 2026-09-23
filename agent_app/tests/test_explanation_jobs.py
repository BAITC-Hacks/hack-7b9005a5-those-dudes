from __future__ import annotations

import csv
from io import BytesIO
import json
from pathlib import Path
from threading import Event, Thread
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

from agent_app.config import AISettings
from agent_app.datasets import DatasetError
from agent_app.explanation_jobs import NarrativeManager, TEXT_FIELDS, _result_valid


IDS = ("100000000343175100", "100000003115284100", "9007199254740993")


def answer(marker="A"):
    return {"evidence": f"{marker}: 3 источника и 2 выхода поддерживают транзит.",
            "why": f"{marker}: 3 источника требуют проверки связи потоков.",
            "explanation": f"{marker}: входы 100 KZT и выходы 99 KZT согласованы; это гипотеза роли.",
            "priority_explanation": f"{marker}: высокий компонент связности определяет очередность проверки.",
            "alternative_explanation": "Регулярные расчёты также могут давать такой профиль.",
            "evidence_json": json.dumps({"facts": [{"metric": "in_deg", "observed": 3}]}),
            "limitations": "Исходящие потоки глубины 4 цензурированы; виновность не устанавливается.",
            "analyst_next_step": "Проверить полноту выходящих потоков.",
            "usage": {"requests": 1, "input_tokens": 100, "output_tokens": 60, "total_tokens": 160}}


class FakeExplanations:
    def __init__(self):
        self.payloads = {gid: {"assigned_role": "transit", "role_score": .7,
                              "priority_score": .9 - index / 10, "metrics": {"in_deg": 3 + index}}
                         for index, gid in enumerate(IDS)}

    def payload(self, gid):
        return self.payloads.get(gid)


class FakeDatasets:
    def __init__(self, root):
        self.root = Path(root)
        self.output = self.root / "output"
        self.output.mkdir()
        self.explanations = FakeExplanations()
        self.write("nodes_roles.csv", ["gid", "role", "role_score", "priority_score", "evidence"],
                   [{"gid": gid, "role": "transit", "role_score": "0.7", "priority_score": str(.9 - index / 10),
                     "evidence": "rule-only"} for index, gid in enumerate(IDS)])
        self.write("top_nodes.csv", ["rank", "gid", "role", "priority_score", "why"],
                   [{"rank": "1", "gid": IDS[1], "role": "transit", "priority_score": "0.8", "why": "priority-rule"}])
        self.write("clusters.csv", ["cluster_id", "top_gids"], [{"cluster_id": "0", "top_gids": "|".join(IDS)}])

    def write(self, name, fields, rows):
        with (self.output / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def context(self, run_id):
        if run_id != "default":
            raise DatasetError("Unknown dataset", 404)
        return None, self.explanations

    def asset(self, run_id, name):
        self.context(run_id)
        return self.output / name


def rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


class NarrativeJobsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.datasets = FakeDatasets(self.temp.name)
        self.preparation = patch("agent_app.explanation_jobs.provider.prepare_payload", side_effect=lambda value: value.copy())
        self.preparation.start()
        self.addCleanup(self.preparation.stop)
        self.settings = patch("agent_app.explanation_jobs.get_ai_settings", return_value=AISettings(api_key="dummy-only", model="test-model"))
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.sdk = patch("agent_app.explanation_jobs.provider.SDK_AVAILABLE", True)
        self.sdk.start()
        self.addCleanup(self.sdk.stop)
        self.generate = AsyncMock(return_value=answer())
        self.generate_patch = patch("agent_app.explanation_jobs.provider.generate_structured_explanation", self.generate)
        self.generate_patch.start()
        self.addCleanup(self.generate_patch.stop)
        self.workers = patch("agent_app.explanation_jobs.BATCH_WORKERS", 1)
        self.workers.start()
        self.addCleanup(self.workers.stop)
        self.manager = NarrativeManager(self.datasets)

    def finish(self):
        thread = self.manager._threads["default"]
        thread.join(10)
        self.assertFalse(thread.is_alive(), "Mocked generation must finish")

    def test_status_and_export_never_call_api_and_preserve_large_ids(self):
        status = self.manager.status("default")
        exported = rows(self.manager.export_file("default", "nodes_roles.csv"))
        self.assertEqual(status["total"], 3)
        self.assertEqual([row["gid"] for row in exported], list(IDS))
        self.assertTrue(all(row["explanation_source"] == "not_generated" for row in exported))
        self.assertTrue(all(row["evidence"] == "rule-only" and row["evidence_rule"] == "rule-only" for row in exported))
        self.assertTrue(all(set(TEXT_FIELDS) <= set(row) for row in exported))
        self.generate.assert_not_called()

    def test_two_hundred_word_paragraphs_survive_csv_and_zip_and_cache(self):
        generated = answer()
        for field in ("evidence", "why"):
            generated[field] = " ".join(["Объяснение,"] * 199 + ['"проверить".'])
        self.generate.return_value = generated
        self.manager.start("default", scope="all")
        self.finish()
        self.assertEqual(self.manager.status("default")["generated_count"], 3)
        reloaded = NarrativeManager(self.datasets)
        for name in ("nodes_roles.csv", "top_nodes.csv"):
            exported = rows(reloaded.export_file("default", name))
            for row in exported:
                for field in ("evidence", "why"):
                    self.assertEqual(row[field], generated[field])
                    self.assertEqual(len(row[field].split()), 200)
            with ZipFile(BytesIO(reloaded.archive("default"))) as archive:
                zipped = list(csv.DictReader(archive.read(name).decode("utf-8-sig").splitlines()))
                self.assertEqual(zipped, exported)
        self.assertEqual(self.generate.await_count, 3)

    def test_cache_contract_rejects_overlong_or_unrendered_paragraphs(self):
        for field in ("evidence", "why"):
            for text in ("слово " * 201, "Не обработана ссылка {{metric.in_deg}}"):
                generated = answer()
                generated[field] = text
                self.assertFalse(_result_valid(generated))

    def test_top_first_generation_and_enrichment_do_not_change_role_scores_or_source_files(self):
        before = {path.name: path.read_bytes() for path in self.datasets.output.iterdir()}
        self.manager.start("default", scope="all")
        self.finish()
        status = self.manager.status("default")
        self.assertEqual((status["status"], status["completed"], status["generated_count"]), ("complete", 3, 3))
        self.assertEqual(self.generate.await_args_list[0].args[0]["metrics"]["in_deg"], 4)
        self.assertEqual(status["usage"]["total_tokens"], 480)
        exported = rows(self.manager.export_file("default", "nodes_roles.csv"))
        top = rows(self.manager.export_file("default", "top_nodes.csv"))
        for row, original in zip(exported, rows(self.datasets.output / "nodes_roles.csv")):
            self.assertEqual(row["gid"], original["gid"])
            self.assertEqual(row["role_score"], original["role_score"])
            self.assertEqual(row["priority_score"], original["priority_score"])
            self.assertEqual(row["role"], original["role"])
            self.assertEqual(row["explanation_source"], "openai")
            self.assertNotEqual(row["evidence"], row["evidence_rule"])
        self.assertEqual(top[0]["why_rule"], "priority-rule")
        self.assertEqual(top[0]["priority_score"], "0.8")
        self.assertEqual(top[0]["explanation"], exported[1]["explanation"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.datasets.output.iterdir()})

    def test_persistent_cache_reuses_results_only_for_same_payload_model_and_version(self):
        first, _ = self.manager.explain_node("default", IDS[0])
        second_manager = NarrativeManager(self.datasets)
        second, _ = second_manager.explain_node("default", IDS[0])
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(self.generate.await_count, 1)
        with patch("agent_app.explanation_jobs.get_ai_settings", return_value=AISettings(api_key="dummy-only", model="other-model")):
            third, _ = second_manager.explain_node("default", IDS[0])
        self.assertFalse(third["cached"])
        self.assertEqual(self.generate.await_count, 2)
        self.datasets.explanations.payloads[IDS[0]]["metrics"]["in_deg"] = 99
        third_manager = NarrativeManager(self.datasets)
        changed, _ = third_manager.explain_node("default", IDS[0])
        self.assertFalse(changed["cached"])
        self.assertEqual(self.generate.await_count, 3)

    def test_cancel_finishes_current_request_and_resume_skips_saved_nodes(self):
        entered, release = Event(), Event()
        async def blocked(*args):
            entered.set()
            release.wait(5)
            return answer()
        self.generate.side_effect = blocked
        self.manager.start("default")
        self.assertTrue(entered.wait(5))
        cancelled = self.manager.cancel("default")
        self.assertTrue(cancelled["cancel_requested"])
        release.set()
        self.finish()
        state = self.manager.status("default")
        self.assertEqual((state["status"], state["completed"], state["pending"]), ("cancelled", 1, 2))
        self.generate.side_effect = None
        self.manager.start("default")
        self.finish()
        self.assertEqual(self.generate.await_count, 3)
        self.assertEqual(self.manager.status("default")["status"], "complete")

    def test_authorization_failure_stops_batch_without_exposing_provider_exception(self):
        error = RuntimeError("secret-key-with-sensitive-provider-data")
        error.status_code = 401
        self.generate.side_effect = error
        self.manager.start("default")
        self.finish()
        status = self.manager.status("default")
        self.assertEqual((status["status"], status["failed"], status["pending"]), ("failed", 1, 2))
        self.assertEqual(self.generate.await_count, 1)
        self.assertNotIn("secret-key", json.dumps(status))
        exported = rows(self.manager.export_file("default", "nodes_roles.csv"))
        self.assertEqual(exported[1]["explanation_source"], "error")
        self.assertNotIn("secret-key", json.dumps(exported))

    def test_bad_schema_is_marked_error_and_never_published_as_openai_success(self):
        self.generate.return_value = {"explanation": "incomplete"}
        response, code = self.manager.explain_node("default", IDS[0])
        self.assertEqual(code, 502)
        self.assertEqual(response["status"], "error")
        self.assertEqual(rows(self.manager.export_file("default", "nodes_roles.csv"))[0]["explanation_source"], "error")

    def test_missing_key_or_sdk_is_explicit_unavailable_and_no_local_success(self):
        with patch("agent_app.explanation_jobs.get_ai_settings", return_value=AISettings()):
            with self.assertRaises(DatasetError) as error:
                self.manager.start("default")
            self.assertEqual(error.exception.status, 503)
            with self.assertRaises(DatasetError):
                self.manager.explain_node("default", IDS[0])
        with patch("agent_app.explanation_jobs.provider.SDK_AVAILABLE", False):
            with self.assertRaises(DatasetError):
                self.manager.start("default")
        self.generate.assert_not_called()

    def test_transient_errors_retry_at_most_once_and_success_is_cached(self):
        transient = TimeoutError("secret connection info")
        self.generate.side_effect = [transient, answer()]
        with patch("agent_app.explanation_jobs.time.sleep"):
            response, code = self.manager.explain_node("default", IDS[0])
        self.assertEqual(code, 200)
        self.assertEqual(self.generate.await_count, 2)
        self.assertEqual(response["mode"], "openai")

    def test_csv_formula_neutralization_and_zip_exactly_three_consistent_files(self):
        unsafe = answer()
        unsafe["explanation"] = '=HYPERLINK("https://invalid.example")'
        unsafe["why"] = "@SUM(1,1)"
        self.generate.return_value = unsafe
        self.manager.start("default")
        self.finish()
        with ZipFile(BytesIO(self.manager.archive("default"))) as archive:
            self.assertEqual(set(archive.namelist()), {"nodes_roles.csv", "top_nodes.csv", "clusters.csv"})
            self.assertEqual(archive.read("clusters.csv"), (self.datasets.output / "clusters.csv").read_bytes())
            for name in archive.namelist():
                self.assertEqual(archive.read(name), self.manager.export_file("default", name).read_bytes())
        exported = rows(self.manager.export_file("default", "nodes_roles.csv"))
        self.assertTrue(exported[0]["explanation"].startswith("'="))
        self.assertTrue(exported[0]["why"].startswith("'@"))
        self.assertEqual(exported[0]["gid"], IDS[0])

    def test_only_one_active_job_and_top_scope_and_limit_are_respected(self):
        entered, release = Event(), Event()
        async def blocked(*args):
            entered.set()
            release.wait(5)
            return answer()
        self.generate.side_effect = blocked
        self.manager.start("default", scope="top", limit=1)
        self.assertTrue(entered.wait(5))
        with self.assertRaises(DatasetError) as error:
            self.manager.start("default")
        self.assertEqual(error.exception.status, 409)
        response, code = self.manager.explain_node("default", IDS[0])
        self.assertEqual(code, 409)
        release.set()
        self.finish()
        state = self.manager.status("default")
        self.assertEqual((state["total"], state["completed"], state["available_nodes"]), (1, 1, 3))

    def test_restart_does_not_resume_api_automatically(self):
        state = self.manager._dataset("default")
        state["status"] = "running"
        state["model"] = "test-model"
        self.manager._save(state)
        restarted = NarrativeManager(self.datasets)
        status = restarted.status("default")
        self.assertEqual(status["status"], "partial")
        self.assertTrue(status["can_resume"])
        self.generate.assert_not_called()

    def test_parallel_workers_are_bounded_and_cancel_waits_for_inflight_only(self):
        entered, release = Event(), Event()
        calls = []
        async def blocked(payload, settings):
            calls.append(payload["metrics"]["in_deg"])
            if len(calls) == 2:
                entered.set()
            release.wait(5)
            return answer()
        self.generate.side_effect = blocked
        with patch("agent_app.explanation_jobs.BATCH_WORKERS", 2):
            self.manager.start("default")
            self.assertTrue(entered.wait(5))
            state = self.manager.cancel("default")
            self.assertEqual(state["inflight"], 2)
            release.set()
            self.finish()
        state = self.manager.status("default")
        self.assertEqual((state["completed"], state["pending"]), (2, 1))
        self.assertEqual(self.generate.await_count, 2)

    def test_invalid_export_and_unknown_run_do_not_access_paths(self):
        for name in ("../.env", "features.csv", "published.json"):
            with self.assertRaises(DatasetError):
                self.manager.export_file("default", name)
        with self.assertRaises(DatasetError):
            self.manager.status("../../secret")
        response, status = self.manager.explain_node("default", "unknown")
        self.assertEqual(status, 404)
        self.generate.assert_not_called()

    def more_nodes(self, count=7):
        records = rows(self.datasets.output / "nodes_roles.csv")
        for index in range(len(records), count):
            gid = str(100000000343175100 + index)
            records.append({"gid": gid, "role": "transit", "role_score": "0.7",
                            "priority_score": "0.5", "evidence": "rule-only"})
            self.datasets.explanations.payloads[gid] = {"assigned_role": "transit", "role_score": .7,
                                                      "priority_score": .5, "metrics": {"in_deg": index + 10}}
        self.datasets.write("nodes_roles.csv", list(records[0]), records)

    def test_failed_validation_usage_is_recorded_and_safe_code_is_exposed(self):
        error = ValueError("INVALID_FACT_REFERENCE")
        error.usage = {"input_tokens": 30, "output_tokens": 20, "total_tokens": 50, "requests": 999}
        self.generate.side_effect = error
        response, code = self.manager.explain_node("default", IDS[0])
        self.assertEqual(code, 502)
        self.assertEqual(response["error_code"], "INVALID_FACT_REFERENCE")
        status = self.manager.status("default")
        self.assertEqual(status["usage"], {"requests": 1, "input_tokens": 30, "output_tokens": 20, "total_tokens": 50})
        self.assertEqual(status["usage_kind"], "recorded_usage_not_billing_estimate")
        exported = rows(self.manager.export_file("default", "nodes_roles.csv"))
        self.assertEqual(exported[0]["explanation_error_code"], "INVALID_FACT_REFERENCE")

    def test_unknown_exception_details_are_not_diagnostics(self):
        self.generate.side_effect = ValueError("secret-key and full sensitive provider payload")
        response, code = self.manager.explain_node("default", IDS[0])
        self.assertEqual(code, 502)
        self.assertEqual(response["error_code"], "ValueError")
        self.assertNotIn("secret-key", json.dumps(response))
        self.assertNotIn("sensitive", json.dumps(self.manager.status("default")))

    def test_three_bad_responses_stop_parallel_batch_and_preserve_reason(self):
        self.more_nodes()
        error = ValueError("INVALID_STRUCTURED_EXPLANATION")
        error.usage = {"input_tokens": 30, "output_tokens": 20, "total_tokens": 50}
        self.generate.side_effect = error
        with patch("agent_app.explanation_jobs.BATCH_WORKERS", 3):
            self.manager.start("default")
            self.finish()
        status = self.manager.status("default")
        self.assertEqual((status["failed"], status["pending"], self.generate.await_count), (3, 4, 3))
        self.assertEqual(status["usage"]["requests"], 3)
        self.assertEqual(status["usage"]["total_tokens"], 150)
        self.assertIn("INVALID_STRUCTURED_EXPLANATION", status["message"])
        self.assertIn("трёх", status["message"])

    def test_three_exhausted_transient_failures_stop_batch_after_six_attempts(self):
        self.more_nodes()
        self.generate.side_effect = ConnectionError("secret service URL")
        with patch("agent_app.explanation_jobs.time.sleep"):
            self.manager.start("default")
            self.finish()
        status = self.manager.status("default")
        self.assertEqual((status["failed"], status["pending"], self.generate.await_count), (3, 4, 6))
        self.assertEqual(status["usage"]["requests"], 6)
        self.assertIn("ConnectionError", status["message"])
        self.assertIn("трёх", status["message"])
        self.assertNotIn("secret", json.dumps(status))

    def test_retry_attempt_usage_counts_without_double_counting_success_requests(self):
        transient = TimeoutError("secret connection info")
        transient.usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        self.generate.side_effect = [transient, answer()]
        with patch("agent_app.explanation_jobs.time.sleep"):
            self.manager.explain_node("default", IDS[0])
        status = self.manager.status("default")
        self.assertEqual(status["usage"], {"requests": 2, "input_tokens": 110, "output_tokens": 65, "total_tokens": 175})

    def test_safe_validation_coordinates_are_saved_and_public(self):
        error = ValueError("INVALID_STRUCTURED_EXPLANATION")
        error.diagnostic = {"field": "evidence_items.interpretation", "rule": "length_out_of_bounds", "length": 12}
        self.generate.side_effect = error
        response, code = self.manager.explain_node("default", IDS[0])
        self.assertEqual(code, 502)
        self.assertIn("evidence_items.interpretation", response["message"])
        self.assertIn("length_out_of_bounds", response["message"])
        self.assertIn("длина: 12", response["message"])
        exported = rows(self.manager.export_file("default", "nodes_roles.csv"))
        self.assertEqual(exported[0]["explanation_error"], response["message"])

    def test_malicious_validation_diagnostics_cannot_leak_text(self):
        cases = (
            {"field": "secret-field-text", "rule": "string_required"},
            {"field": "explanation", "rule": "secret-rule-text"},
            {"field": "explanation", "rule": "length_out_of_bounds", "length": "secret-length-text", "value": "secret-model-prose"},
            {"field": {"secret": "dictionary"}, "rule": "string_required"},
            {"field": "explanation", "rule": "length_out_of_bounds", "length": 100_001},
            {"field": "explanation", "rule": "length_out_of_bounds", "length": True},
        )
        for diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                error = ValueError("INVALID_STRUCTURED_EXPLANATION")
                error.diagnostic = diagnostic
                self.generate.side_effect = error
                response, code = self.manager.explain_node("default", IDS[0])
                self.assertEqual(code, 502)
                self.assertNotIn("secret", json.dumps(response))
                self.assertNotIn("длина:", response["message"])
                self.assertNotIn("secret", json.dumps(rows(self.manager.export_file("default", "nodes_roles.csv"))))


if __name__ == "__main__":
    unittest.main()
