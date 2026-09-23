from __future__ import annotations

import asyncio
from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_app import agent
from agent_app.config import AISettings, get_ai_settings
from agent_app.engine import InvestigationEngine
from agent_app.explanations import RoleExplanationService
from agent_app.main import AssistantHandler
from agent_app.store import GraphDataStore

ROOT = Path(__file__).resolve().parents[2]


class SettingsTest(unittest.TestCase):
    def test_dotenv_reloads_and_environment_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text('OPENAI_API_KEY="dummy-one"\nOPENAI_MODEL=gpt-4.1-mini\n', encoding="utf-8")
            self.assertEqual(get_ai_settings(directory).api_key, "dummy-one")
            path.write_text('OPENAI_API_KEY="dummy-two"\nOPENAI_TIMEOUT_SECONDS=invalid\n', encoding="utf-8")
            self.assertEqual(get_ai_settings(directory).api_key, "dummy-two")
            self.assertEqual(get_ai_settings(directory).timeout_seconds, 45)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "environment-key"}):
                self.assertEqual(get_ai_settings(directory).api_key, "environment-key")
            self.assertNotIn("dummy-two", repr(get_ai_settings(directory)))


class ExplanationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = InvestigationEngine(GraphDataStore(root=ROOT, strict_outputs=True))

    def setUp(self):
        self.service = RoleExplanationService(self.engine)
        self.gid = self.engine.store.roles.iloc[0]["gid"]

    def test_all_saved_role_decisions_match_the_explanation_rules(self):
        roles = set()
        for gid in self.engine.store.node_ids:
            payload = self.service.payload(gid)
            trace = payload["decision_trace"]
            self.assertTrue(trace["available"], gid)
            eligible = {role: item["profile_score"] for role, item in trace["roles"].items() if item["eligible"]}
            selected = max(trace["role_order"], key=lambda role: eligible.get(role, float("-inf")))
            self.assertEqual(selected, payload["assigned_role"], gid)
            roles.add(selected)
        self.assertEqual(len(roles), 6)

    def test_prompt_contains_no_client_identifiers_or_raw_transactions(self):
        payload = self.service.payload(self.gid)
        text = json.dumps(payload)
        self.assertNotIn(self.gid, text)
        self.assertNotIn('"gid"', text)
        self.assertNotIn('"src"', text)
        self.assertNotIn('"dst"', text)
        self.assertNotIn('"transactions"', text)
        self.assertIn("assigned_role", payload)
        self.assertIn("decision_trace", payload)

    def test_no_key_gives_labeled_local_explanation_without_api_call(self):
        with patch("agent_app.explanations.get_ai_settings", return_value=AISettings()), patch.object(agent, "generate_role_explanation", new_callable=AsyncMock) as generate:
            reply, code = self.service.explain(self.gid)
        self.assertEqual(code, 200)
        self.assertEqual(reply["mode"], "offline")
        self.assertIn(".env", reply["notice"])
        self.assertIn(reply["role"], reply["explanation"])
        generate.assert_not_called()

    def test_frontier_explanation_always_mentions_censoring(self):
        gid = next(gid for gid in self.engine.store.node_ids if self.engine.store.node_row(gid)["depth"] == 4)
        answer = self.service.local_explanation(self.service.payload(gid))
        self.assertIn("цензурированы", answer)
        self.assertIn("не доказывает terminal", answer)

    def test_sdk_success_is_cached_and_does_not_change_classification(self):
        original = self.engine.store.node_row(self.gid)
        with patch("agent_app.explanations.get_ai_settings", return_value=AISettings(api_key="dummy-only")), patch.object(agent, "SDK_AVAILABLE", True), patch.object(agent, "generate_role_explanation", new_callable=AsyncMock, return_value="Test explanation") as generate:
            first, code = self.service.explain(self.gid)
            second, _ = self.service.explain(self.gid)
        self.assertEqual(code, 200)
        self.assertEqual(first["mode"], "openai")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(self.engine.store.node_row(self.gid), original)

    def test_provider_errors_do_not_expose_secrets(self):
        failure = RuntimeError("secret-test-key must never leave backend")
        failure.status_code = 401
        with patch("agent_app.explanations.get_ai_settings", return_value=AISettings(api_key="secret-test-key")), patch.object(agent, "SDK_AVAILABLE", True), patch.object(agent, "generate_role_explanation", new_callable=AsyncMock, side_effect=failure):
            reply, _ = self.service.explain(self.gid)
        self.assertTrue(reply["api_error"])
        self.assertEqual(reply["mode"], "offline")
        self.assertNotIn("secret-test-key", json.dumps(reply))
        self.assertIn("ключ не принят", reply["notice"])

    def test_unknown_node_does_not_invoke_provider(self):
        with patch.object(agent, "generate_role_explanation", new_callable=AsyncMock) as generate:
            reply, status = self.service.explain("0")
        self.assertEqual(status, 404)
        generate.assert_not_called()

    def test_busy_request_has_clear_retry_response(self):
        with patch("agent_app.explanations.get_ai_settings", return_value=AISettings(api_key="dummy-only")), patch.object(agent, "SDK_AVAILABLE", True):
            with self.service.lock:
                result, code = self.service.explain(self.gid)
        self.assertEqual(code, 429)
        self.assertEqual(result["status"], "busy")

    @unittest.skipUnless(agent.SDK_AVAILABLE, "Optional SDK not installed")
    def test_real_sdk_adapter_sends_only_prepared_evidence_without_tools_or_traces(self):
        payload = self.service.payload(self.gid)
        with patch.object(agent.Runner, "run", new_callable=AsyncMock, return_value=SimpleNamespace(final_output="Тестовый ответ")) as run:
            answer = asyncio.run(agent.generate_role_explanation(payload, AISettings(api_key="dummy-only")))
        self.assertEqual(answer, "Тестовый ответ")
        sdk_agent, prompt = run.call_args.args
        self.assertEqual(sdk_agent.tools, [])
        self.assertEqual(json.loads(prompt), payload)
        self.assertFalse(sdk_agent.model_settings.store)
        self.assertTrue(run.call_args.kwargs["run_config"].tracing_disabled)


class ExplanationHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = InvestigationEngine(GraphDataStore(root=ROOT, strict_outputs=True))
        cls.gid = cls.engine.store.roles.iloc[0]["gid"]
        cls.directory = tempfile.TemporaryDirectory()
        cls.private = Path(cls.directory.name) / ".env"
        cls.private.write_text("OPENAI_API_KEY=dummy-private-fixture", encoding="utf-8")
        class Handler(AssistantHandler):
            def log_message(self, *args):
                pass
        Handler.engine = cls.engine
        Handler.explanations = RoleExplanationService(cls.engine)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=cls.directory.name))
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.directory.cleanup()

    def request(self, path, payload=None, headers=None):
        request = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                          headers=headers or {})
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, response.read().decode()
        except HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_button_endpoint_returns_explanation_for_exact_string_gid(self):
        with patch("agent_app.explanations.get_ai_settings", return_value=AISettings()):
            code, body = self.request("/api/explain-node", {"gid": self.gid}, {"Content-Type": "application/json", "X-HackAlem-Action": "explain-node", "Origin": self.base})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["gid"], self.gid)
        self.assertEqual(json.loads(body)["mode"], "offline")

    def test_external_origin_cannot_trigger_generation(self):
        code, _ = self.request("/api/explain-node", {"gid": self.gid}, {"Content-Type": "application/json", "X-HackAlem-Action": "explain-node", "Origin": "https://unrelated.example"})
        self.assertEqual(code, 403)

    def test_numeric_gid_is_rejected_before_precision_can_be_lost(self):
        code, _ = self.request("/api/explain-node", {"gid": int(self.gid)}, {"Content-Type": "application/json", "X-HackAlem-Action": "explain-node"})
        self.assertEqual(code, 400)

    def test_dotenv_and_encoded_variants_are_not_served(self):
        for path in ("/.env", "/%2eenv", "/../.env", "/%2e%2e/.env"):
            code, body = self.request(path)
            self.assertEqual(code, 404)
            self.assertNotIn("dummy-private-fixture", body)

    def test_status_does_not_include_key(self):
        with patch("agent_app.explanations.get_ai_settings", return_value=AISettings(api_key="private-test-key")):
            code, body = self.request("/api/ai-status")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["configured"])
        self.assertNotIn("private-test-key", body)


if __name__ == "__main__":
    unittest.main()
