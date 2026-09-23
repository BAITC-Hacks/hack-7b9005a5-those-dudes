"""Consent, dataset isolation and enriched-export HTTP contract; no API calls."""
from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_app.datasets import DatasetError
from agent_app.main import AssistantHandler
from agent_app.tests.test_dataset_http import FakeDatasets


class FakeNarratives:
    def __init__(self, datasets):
        self.datasets = datasets
        self.calls = []

    def status(self, run_id):
        self.datasets.status(run_id)
        return {"status": "idle", "run_id": run_id, "generated_count": 0, "available_nodes": 3}

    def start(self, run_id, scope="all", limit=None):
        self.calls.append(("start", run_id, scope, limit))
        return {"status": "running", "run_id": run_id, "total": limit or 3}

    def cancel(self, run_id):
        self.calls.append(("cancel", run_id))
        return {"status": "cancelled", "run_id": run_id}

    def explain_node(self, run_id, gid):
        self.calls.append(("node", run_id, gid))
        return {"mode": "openai", "gid": gid, "explanation": "Structured explanation", "run_id": run_id}, 200

    def export_file(self, run_id, name):
        self.calls.append(("export", run_id, name))
        return self.datasets.asset(run_id, name)

    def archive(self, run_id):
        self.calls.append(("archive", run_id))
        return self.datasets.archive(run_id)


class NarrativeHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        for name in ("nodes_roles.csv", "clusters.csv", "top_nodes.csv"):
            (cls.directory / name).write_text("gid,explanation\n123,test\n", encoding="utf-8")

        class Handler(AssistantHandler):
            def log_message(self, *args):
                pass

        Handler.datasets = FakeDatasets(cls.directory)
        cls.narratives = Handler.narratives = FakeNarratives(Handler.datasets)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(cls.directory)))
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def setUp(self):
        self.narratives.calls.clear()

    def request(self, path, body=None, headers=None):
        headers = {"Content-Type": "application/json", "X-HackAlem-Action": "explain-batch",
                   "Origin": self.base, **(headers or {})}
        try:
            response = urlopen(Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                       headers=headers), timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read()

    def test_status_is_read_only_and_returns_configuration(self):
        code, body = self.request("/api/explanations/status?run=default")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["generated_count"], 0)
        self.assertFalse(json.loads(body)["configured"])
        self.assertEqual(self.narratives.calls, [])

    def test_explicit_consent_and_same_origin_header_required(self):
        route = "/api/explanations/start?run=default"
        for consent in (False, "true", None):
            self.assertEqual(self.request(route, {"consent": consent})[0], 400)
        self.assertEqual(self.request(route, {"consent": True}, {"Origin": "https://unrelated.example"})[0], 403)
        self.assertEqual(self.request(route, {"consent": True}, {"X-HackAlem-Action": ""})[0], 403)
        self.assertEqual(self.narratives.calls, [])

    def test_start_cancel_and_single_node_keep_selected_run(self):
        run = "a" * 32
        self.assertEqual(self.request("/api/explanations/start?run=" + run,
                                      {"consent": True, "scope": "top", "limit": 2})[0], 202)
        self.assertEqual(self.narratives.calls[-1], ("start", run, "top", 2))
        self.assertEqual(self.request("/api/explanations/cancel?run=" + run, {})[0], 200)
        self.assertEqual(self.narratives.calls[-1], ("cancel", run))
        code, body = self.request("/api/explain-node?run=" + run, {"gid": "9007199254740993"},
                                  {"X-HackAlem-Action": "explain-node"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["mode"], "openai")
        self.assertEqual(self.narratives.calls[-1], ("node", run, "9007199254740993"))

    def test_unknown_run_does_not_start_default_job(self):
        self.assertEqual(self.request("/api/explanations/start?run=missing", {"consent": True})[0], 404)
        self.assertEqual(self.narratives.calls, [])

    def test_original_export_buttons_use_enriched_snapshots_without_starting_api(self):
        for name in ("nodes_roles.csv", "top_nodes.csv", "clusters.csv"):
            self.assertEqual(self.request("/api/datasets/default/exports/" + name)[0], 200)
            self.assertEqual(self.narratives.calls[-1], ("export", "default", name))
        self.assertEqual(self.request("/api/datasets/default/exports/results.zip")[0], 200)
        self.assertEqual(self.narratives.calls[-1], ("archive", "default"))
        self.assertFalse(any(call[0] == "start" for call in self.narratives.calls))


if __name__ == "__main__":
    unittest.main()
