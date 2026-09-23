"""HTTP contract tests: no pipeline, external model, or real client data needed."""
from functools import partial
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
from threading import Thread
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from agent_app.datasets import DatasetError
from agent_app.main import AssistantHandler


EXPORTS = ("nodes_roles.csv", "clusters.csv", "top_nodes.csv")


def multipart(parts):
    body = bytearray()
    for name, filename, payload in parts:
        body.extend(f'--test-boundary\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
        body.extend(payload)
        body.extend(b"\r\n")
    body.extend(b"--test-boundary--\r\n")
    return bytes(body)


class FakeDatasets:
    def __init__(self, directory):
        self.directory = directory
        self.submissions = []
        self.contexts = []

    def status(self, run_id):
        if run_id not in {"default", "a" * 32}:
            raise DatasetError("Набор не найден.", 404)
        return {"run_id": run_id, "status": "ready", "dashboard_url": "/dashboard?run=" + run_id}

    def current(self):
        return self.status("default")

    def submit(self, files):
        self.submissions.append(files)
        return {"run_id": "a" * 32, "status": "queued", "status_url": "/api/datasets/" + "a" * 32}

    def context(self, run_id):
        self.status(run_id)
        self.contexts.append(run_id)
        engine = SimpleNamespace(health=lambda: {"run_id": run_id},
                                 execute=lambda body: {"status": "ok", "run_id": run_id},
                                 answer_offline=lambda question: {"status": "ok", "run_id": run_id})
        explanations = SimpleNamespace(status=lambda: {"configured": False},
                                       explain=lambda gid: ({"gid": gid, "run_id": run_id}, 200))
        return engine, explanations

    def asset(self, run_id, name):
        self.status(run_id)
        if name not in (*EXPORTS, "dashboard_data.js"):
            raise DatasetError("Файл не найден.", 404)
        return self.directory / name

    def archive(self, run_id):
        self.status(run_id)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name in EXPORTS:
                archive.write(self.asset(run_id, name), name)
        return stream.getvalue()


class DatasetHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        for name, body in {"upload.html": "UPLOAD PAGE", "index.html": "DASHBOARD PAGE",
                           "dashboard_data.js": "window.HACKALEM_DATA={};",
                           **{name: "gid,value\n123,test\n" for name in EXPORTS}}.items():
            (cls.directory / name).write_text(body, encoding="utf-8")

        class Handler(AssistantHandler):
            def log_message(self, *args):
                pass

        cls.manager = FakeDatasets(cls.directory)
        Handler.datasets = cls.manager
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

    def request(self, path, body=None, headers=None):
        try:
            response = urlopen(Request(self.base + path, data=body, headers=headers or {}), timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.headers, response.read()

    def upload(self, parts=None, headers=None):
        defaults = {"Content-Type": "multipart/form-data; boundary=test-boundary",
                    "X-HackAlem-Action": "upload", "Origin": self.base}
        defaults.update(headers or {})
        if parts is None:
            parts = [(name, name + ".parquet", b"PAR1" + name.encode() + b"PAR1")
                     for name in ("nodes", "edges", "transactions")]
        return self.request("/api/datasets", multipart(parts), defaults)

    def test_root_starts_at_upload_and_dashboard_is_separate(self):
        for route in ("/", "/index.html"):
            self.assertEqual(self.request(route)[2], b"UPLOAD PAGE")
        self.assertEqual(self.request("/dashboard?run=default")[2], b"DASHBOARD PAGE")

    def test_upload_preserves_binary_bytes_and_returns_job(self):
        payload = b"PAR1\x00\xff\r\n\x80PAR1"
        code, _, body = self.upload([(name, name + ".parquet", payload)
                                     for name in ("nodes", "edges", "transactions")])
        self.assertEqual(code, 202)
        self.assertEqual(json.loads(body)["status"], "queued")
        self.assertEqual(self.manager.submissions[-1], dict.fromkeys(("nodes", "edges", "transactions"), payload))

    def test_missing_duplicate_and_wrong_extension_are_rejected(self):
        bad = [
            [("nodes", "nodes.parquet", b"x")],
            [("nodes", "nodes.parquet", b"x")] * 3,
            [(name, name + ".csv", b"x") for name in ("nodes", "edges", "transactions")],
        ]
        count = len(self.manager.submissions)
        for parts in bad:
            self.assertEqual(self.upload(parts)[0], 400)
        self.assertEqual(len(self.manager.submissions), count)

    def test_external_origin_and_missing_action_header_rejected(self):
        self.assertEqual(self.upload(headers={"Origin": "https://other.example"})[0], 403)
        self.assertEqual(self.upload(headers={"X-HackAlem-Action": ""})[0], 403)
        self.assertEqual(self.upload(headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_malformed_multipart_returns_json_error(self):
        code, headers, _ = self.request("/api/datasets", b"garbage", {
            "Content-Type": "multipart/form-data; boundary=broken", "X-HackAlem-Action": "upload"})
        self.assertEqual(code, 400)
        self.assertIn("application/json", headers["Content-Type"])

    def test_status_bundle_and_individual_csv(self):
        code, _, body = self.request("/api/datasets/current")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["run_id"], "default")
        self.assertEqual(self.request("/api/datasets/default/dashboard_data.js")[0], 200)
        for name in EXPORTS:
            code, headers, body = self.request("/api/datasets/default/exports/" + name)
            self.assertEqual(code, 200)
            self.assertIn('filename="' + name + '"', headers["Content-Disposition"])
            self.assertEqual(body, (self.directory / name).read_bytes())
        self.assertEqual(self.request("/api/datasets/default/exports/.env")[0], 404)

    def test_zip_contains_exactly_three_full_exports(self):
        code, headers, body = self.request("/api/datasets/default/exports/results.zip")
        self.assertEqual(code, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(set(archive.namelist()), set(EXPORTS))
            for name in EXPORTS:
                self.assertEqual(archive.read(name), (self.directory / name).read_bytes())

    def test_queries_health_and_explanations_pin_selected_dataset(self):
        selected = "a" * 32
        code, _, body = self.request("/health?run=" + selected)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["run_id"], selected)
        for route in ("query", "action", "explain-node"):
            code, _, body = self.request("/api/" + route + "?run=" + selected,
                                         json.dumps({"gid": "9007199254740993", "question": "test"}).encode(),
                                         {"Content-Type": "application/json", "X-HackAlem-Action": "explain-node"})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["run_id"], selected)

    def test_unknown_dataset_never_falls_back_to_original(self):
        self.assertEqual(self.request("/health?run=unknown")[0], 404)
        self.assertEqual(self.request("/health?run=default&run=unknown")[0], 400)
        self.assertEqual(self.request("/api/datasets/unknown")[0], 404)


if __name__ == "__main__":
    unittest.main()
