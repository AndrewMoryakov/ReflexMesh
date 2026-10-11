"""Decision providers for the browser path: the script stand-in, the protocol validator and the
real SOH provider class over a local fake of the decisions endpoint (wire and budget only).

The fake server proves the request/response plumbing and that no hidden retry escapes the call
budget. It says nothing about how well Jev chooses.
"""

import importlib.util
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

SOH = importlib.util.find_spec("systemone_harness") is not None

if SOH:
    from systemone_harness.provider import OpenRouterProvider, ProviderError
    from reflexmesh.adapters.system_one.decision_providers import (
        LabelScriptProvider, ProviderUnavailable, ValidatingProvider, jev_provider_from_env)

QUESTIONS = {
    "next_action": {"type": "choice", "instructions": "x", "criteria": {"click": "c", "type_text": "t", "finish": "f"}},
    "goal_reached": {"type": "noul", "instructions": "x"},
    "click__element": {"type": "choice", "instructions": "x", "criteria": {"26": "button 'Search'", "62": "link 'Заказ №125'"}},
    "type_text__field": {"type": "choice", "instructions": "x", "criteria": {"25": "textbox 'Order number' (empty)"}},
    "type_text__value": {"type": "choice", "instructions": "x", "criteria": {"order@1": "order@1: 125"}},
}


def answers(action="click", element="26", **extra):
    def choice(pick, criteria):
        return {"type": "choice", "choice": pick, "confidence": 0.9,
                "probabilities": {k: (0.9 if k == pick else 0.0) for k in criteria}}

    base = {"next_action": choice(action, QUESTIONS["next_action"]["criteria"]),
            "goal_reached": {"type": "noul", "noul": 0.1},
            "click__element": choice(element, QUESTIONS["click__element"]["criteria"]),
            "type_text__field": choice("25", QUESTIONS["type_text__field"]["criteria"]),
            "type_text__value": choice("order@1", QUESTIONS["type_text__value"]["criteria"])}
    base.update(extra)
    return base


class FixedProvider:
    model = "fixed"

    def __init__(self, reply):
        self.reply = reply

    def decide(self, state, questions):
        from systemone_harness.provider import Decision
        return Decision(answers=self.reply, usage={}, model="fixed", request_id="r", latency_ms=1)


@unittest.skipUnless(SOH, "systemone_harness is not installed")
class ScriptProvider(unittest.TestCase):
    def test_picks_options_by_their_visible_description(self):
        provider = LabelScriptProvider([{"action": "type_text", "field": "Order number", "value": "^order@1"},
                                        {"action": "click", "element": "Заказ №125"}])
        first = provider.decide({}, QUESTIONS).answers
        self.assertEqual(first["next_action"]["choice"], "type_text")
        self.assertEqual(first["type_text__field"]["choice"], "25")
        self.assertEqual(first["type_text__value"]["choice"], "order@1")
        self.assertEqual(provider.decide({}, QUESTIONS).answers["click__element"]["choice"], "62")
        self.assertEqual(provider.decide({}, QUESTIONS).answers["next_action"]["choice"], "finish")

    def test_a_missing_or_ambiguous_match_fails_loudly(self):
        with self.assertRaises(ProviderError):
            LabelScriptProvider([{"action": "click", "element": "Nonexistent"}]).decide({}, QUESTIONS)
        with self.assertRaises(ProviderError):
            LabelScriptProvider([{"action": "click", "element": "."}]).decide({}, QUESTIONS)
        with self.assertRaises(ProviderError):
            LabelScriptProvider([{"action": "scroll"}]).decide({}, QUESTIONS)


@unittest.skipUnless(SOH, "systemone_harness is not installed")
class Validation(unittest.TestCase):
    def check(self, reply):
        return ValidatingProvider(FixedProvider(reply)).decide({}, QUESTIONS)

    def test_a_well_formed_answer_passes(self):
        self.assertEqual(self.check(answers()).model, "fixed")

    def test_an_operation_that_was_not_offered_is_a_protocol_error(self):
        with self.assertRaises(ProviderError) as caught:
            self.check(answers(action="evaluate"))
        self.assertIn("protocol", str(caught.exception))

    def test_a_target_or_value_that_was_not_offered_is_a_protocol_error(self):
        with self.assertRaises(ProviderError):
            self.check(answers(element="999"))
        reply = answers(action="type_text")
        reply["type_text__value"] = {"type": "choice", "choice": "order@2", "confidence": 0.9, "probabilities": {}}
        with self.assertRaises(ProviderError):
            self.check(reply)
        reply = answers(action="type_text")
        del reply["type_text__field"]
        with self.assertRaises(ProviderError):
            self.check(reply)

    def test_non_finite_or_out_of_range_numbers_are_rejected(self):
        for bad in (float("nan"), float("inf"), 1.5, -0.1, "0.9"):
            reply = answers()
            reply["next_action"]["confidence"] = bad
            with self.assertRaises(ProviderError, msg=repr(bad)):
                self.check(reply)
        reply = answers()
        reply["click__element"]["probabilities"]["26"] = float("nan")
        with self.assertRaises(ProviderError):
            self.check(reply)

    def test_unselected_actions_may_hold_anything_without_effect(self):
        reply = answers(element="26")
        reply["type_text__field"] = {"type": "choice", "choice": "zzz", "confidence": 0.2, "probabilities": {}}
        self.check(reply)

    def test_finish_and_escalate_need_no_parameters(self):
        questions = {**QUESTIONS, "next_action": {**QUESTIONS["next_action"],
                                                  "criteria": {"click": "c", "finish": "f", "escalate": "e"}}}
        reply = {"next_action": {"type": "choice", "choice": "escalate", "confidence": 0.9, "probabilities": {}},
                 "goal_reached": {"type": "noul", "noul": 0.0}}
        self.assertEqual(ValidatingProvider(FixedProvider(reply)).decide({}, questions).answers["next_action"]["choice"],
                         "escalate")


class DecisionsHandler(BaseHTTPRequestHandler):
    requests: list = []
    status = 200
    model = "typesafe/jev-served-1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"path": self.path, "body": body, "auth": self.headers.get("authorization")})
        if type(self).status != 200:
            self.send_response(type(self).status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps({"answers": answers(), "model": type(self).model, "id": "req-1",
                           "usage": {"input_tokens": 7, "output_tokens": 3}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(SOH, "systemone_harness is not installed")
class JevProviderWire(unittest.TestCase):
    def setUp(self):
        DecisionsHandler.requests, DecisionsHandler.status = [], 200
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DecisionsHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def provider(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test-not-a-real-key", "S1_MODEL": "~typesafe/jev-latest"}):
            provider, config_id = jev_provider_from_env()
        provider.base_url = self.base
        return provider, config_id

    def test_factory_builds_the_decisions_endpoint_client_with_retries_off(self):
        provider, config_id = self.provider()
        self.assertIsInstance(provider, OpenRouterProvider)
        self.assertEqual(provider.retries, 0)
        self.assertEqual(provider.path, "/api/alpha/decisions")
        self.assertEqual(config_id, "openrouter:~typesafe/jev-latest")

    def test_without_a_key_the_provider_is_unavailable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProviderUnavailable):
                jev_provider_from_env()

    def test_one_request_per_decision_with_requested_and_served_model(self):
        provider, _ = self.provider()
        decision = ValidatingProvider(provider).decide({"goal": "g"}, QUESTIONS)
        self.assertEqual(len(DecisionsHandler.requests), 1)
        request = DecisionsHandler.requests[0]
        self.assertEqual(request["path"], "/api/alpha/decisions")
        self.assertEqual(request["body"]["model"], "~typesafe/jev-latest")
        self.assertEqual(request["body"]["questions"]["next_action"]["criteria"], QUESTIONS["next_action"]["criteria"])
        self.assertEqual(decision.model, "typesafe/jev-served-1")  # served, not requested
        self.assertEqual(decision.usage, {"input_tokens": 7, "output_tokens": 3})

    def test_a_server_error_is_not_retried_behind_the_budget(self):
        DecisionsHandler.status = 503
        provider, _ = self.provider()
        with self.assertRaises(ProviderError):
            provider.decide({}, QUESTIONS)
        self.assertEqual(len(DecisionsHandler.requests), 1)


if __name__ == "__main__":
    unittest.main()
