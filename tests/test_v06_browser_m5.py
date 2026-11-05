"""The comparison of section 17: mode L's provider over a fake endpoint, and the runner's bookkeeping.

No model and no browser: this proves the plumbing and the arithmetic, and says nothing about which mode is better.
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

SOH = importlib.util.find_spec("systemone_harness") is not None
spec = importlib.util.spec_from_file_location("m5_runner", ROOT / "experiments/v06/m5/runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

QUESTIONS = {
    "next_action": {"type": "choice", "instructions": "x", "criteria": {"click": "c", "type_text": "t", "finish": "f", "escalate": "e"}},
    "goal_reached": {"type": "noul", "instructions": "x"},
    "click__element": {"type": "choice", "instructions": "x", "criteria": {"26": "button 'Search'"}},
    "type_text__field": {"type": "choice", "instructions": "x", "criteria": {"25": "textbox 'Order number' (empty)"}},
    "type_text__value": {"type": "choice", "instructions": "x", "criteria": {"order@1": "order@1: 125"}},
}


@unittest.skipUnless(SOH, "SystemOneHarness is not installed")
class ChatProvider(unittest.TestCase):
    def setUp(self):
        self.seen, self.reply, self.status = [], {}, 200
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                case.seen.append((self.path, self.headers.get("authorization"), body))
                content = case.reply if type(case.reply) is str else json.dumps(case.reply)
                data = json.dumps({"id": "gen-1", "model": "fake/served", "usage": {"prompt_tokens": 7, "completion_tokens": 3, "cost": 0.5},
                                   "choices": [{"message": {"content": content}}]}).encode()
                self.send_response(case.status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        from reflexmesh.adapters.system_one.decision_providers import ChatChoiceProvider, ChatClient, ValidatingProvider

        self.provider = ValidatingProvider(ChatChoiceProvider(ChatClient("sk-test", "fake/model", 5, f"http://127.0.0.1:{self.server.server_port}")))
        self.addCleanup(self.provider.close)

    def test_a_choice_becomes_the_wire_answers_with_usage_and_cost(self):
        self.reply = {"next_action": "type_text", "type_text__field": "25", "type_text__value": "order@1", "goal_reached": 0.2}
        decision = self.provider.decide({"goal": "g"}, QUESTIONS)
        self.assertEqual(decision.answers["next_action"]["choice"], "type_text")
        self.assertEqual(decision.answers["type_text__value"]["choice"], "order@1")
        self.assertEqual(decision.answers["goal_reached"]["noul"], 0.2)
        self.assertEqual(decision.usage, {"input_tokens": 7, "output_tokens": 3, "cost": 0.5})
        self.assertEqual((decision.model, decision.request_id), ("fake/served", "gen-1"))
        path, auth, body = self.seen[0]
        self.assertEqual((path, auth, body["model"]), ("/api/v1/chat/completions", "Bearer sk-test", "fake/model"))
        self.assertEqual(len(self.seen), 1)

    def test_what_was_not_offered_is_a_protocol_error_not_a_guess(self):
        from systemone_harness.provider import ProviderError

        for reply in ({"next_action": "navigate"}, {"next_action": "click", "click__element": "99"}, {"next_action": "click"},
                      "not json", ["a list"]):
            self.reply = reply
            with self.assertRaisesRegex(ProviderError, "protocol"):
                self.provider.decide({}, QUESTIONS)

    def test_formatting_around_one_object_is_read_and_two_objects_are_refused(self):
        """All three forms came from a real model on a route that does not honour `response_format`."""
        from systemone_harness.provider import ProviderError

        one = '{"next_action": "click", "click__element": "26", "goal_reached": 0.1}'
        for text in (one, f"```json\n{one}\n```", f"The team is set, so go on.\n\n```json\n{one}\n```", f"\n<invoke:\n{one}"):
            self.reply = text
            self.assertEqual(self.provider.decide({}, QUESTIONS).answers["click__element"]["choice"], "26")
        self.reply = '{"next_action": "click", "click__element": "26"} or {"next_action": "finish"}'
        with self.assertRaisesRegex(ProviderError, "protocol.*2 JSON objects"):
            self.provider.decide({}, QUESTIONS)
        self.reply = '{"next_action": {"click__element": "26"}, "goal_reached": 0}'   # the action nested: a model error
        with self.assertRaisesRegex(ProviderError, "protocol"):
            self.provider.decide({}, QUESTIONS)

    def test_a_fenced_reply_is_read_and_a_server_error_is_not_retried(self):
        from systemone_harness.provider import ProviderError

        self.reply = '```json\n{"next_action": "finish"}\n```'
        self.assertEqual(self.provider.decide({}, QUESTIONS).answers["goal_reached"]["noul"], 0.95)
        self.status, count = 500, len(self.seen)
        with self.assertRaises(ProviderError):
            self.provider.decide({}, QUESTIONS)
        self.assertEqual(len(self.seen), count + 1)

    def test_there_is_no_default_model_and_no_key_means_unavailable(self):
        from reflexmesh.adapters.system_one.decision_providers import ProviderUnavailable, chat_provider_from_env

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}), self.assertRaises(ProviderUnavailable):
            chat_provider_from_env(None)
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ProviderUnavailable):
            chat_provider_from_env("fake/model")


class Runner(unittest.TestCase):
    MANIFEST = {"kind": "protocol_check", "repeats": 3, "tasks": [{"id": "a"}, {"id": "b"}]}

    def test_every_run_is_planned_and_no_mode_always_goes_first(self):
        runs = runner.plan(self.MANIFEST)
        self.assertEqual(len(runs), 18)
        self.assertEqual({r["mode"] for r in runs if r["task"]["id"] == "a" and r["repeat"] == 1}, set(runner.MODES))
        firsts = [next(r["mode"] for r in runs if r["repeat"] == n) for n in (1, 2, 3)]
        self.assertEqual(sorted(firsts), sorted(runner.MODES))

    def test_the_oracle_counts_one_correct_note_and_nothing_else(self):
        oracle = {"kind": "order_desk_note", "order": "125", "note_slot": "note"}
        task = {"text_slots": [{"id": "note", "value": "N"}]}
        self.assertEqual(runner.judge(oracle, task, {"notes": {"125": ["N"]}}), (True, "one_correct_note"))
        self.assertEqual(runner.judge(oracle, task, {"notes": {}}), (False, "no_note"))
        self.assertEqual(runner.judge(oracle, task, {"notes": {"125": ["N", "N"]}}), (False, "wrong_notes"))
        self.assertEqual(runner.judge(oracle, task, {"notes": {"125": ["X"]}}), (False, "wrong_notes"))
        self.assertEqual(runner.judge(oracle, task, None), (None, "oracle_unavailable"))

    def test_a_failed_run_stays_in_the_denominator_and_unknown_cost_is_not_a_saving(self):
        rows = [{"mode": "L", "oracle_success": True, "attempt_status": "completed", "cost": 0.1, "wall_seconds": 10, "steps": 8, "model_calls": 5},
                {"mode": "L", "category": "infra", "oracle_success": False, "cost": None, "wall_seconds": 1}]
        s = runner.summarize(rows)["L"]
        self.assertEqual((s["runs"], s["confirmed_success"], s["infra_failures"]), (2, 1, 1))
        self.assertIsNone(s["cost"])
        self.assertEqual(runner.summarize(rows[:1])["L"]["cost"], 0.1)

    def test_a_claim_the_oracle_denies_is_a_false_completion(self):
        s = runner.summarize([{"mode": "J0", "attempt_status": "completed", "oracle_success": False, "false_completion": True}])["J0"]
        self.assertEqual((s["claimed_completed"], s["confirmed_success"], s["false_completion"]), (1, 0, 1))

    def test_the_real_site_oracle_needs_the_url_and_every_text(self):
        o = {"final_url": r"/done$", "contains": ["a b", "c"], "where": "page"}
        state = {"url": "https://x/done", "title": "", "text": "a b and c", "textareas": "", "frames": [], "frame_values": [], "shadow_text": "", "shadow_checked": 0}
        self.assertEqual(runner.judge_final(o, state), (True, "matched"))
        self.assertEqual(runner.judge_final(o, {**state, "url": "https://x/start"}), (False, "url_differs"))
        self.assertEqual(runner.judge_final(o, {**state, "text": "a b"}), (False, "text_missing"))
        self.assertEqual(runner.judge_final(o, None), (None, "oracle_unavailable"))
        self.assertEqual(runner.judge_final({"final_url": "x", "contains": ["k"], "where": "frame"}, {**state, "url": "x", "text": "k", "frames": ["other"]}), (False, "text_missing"))
        self.assertEqual(runner.judge_final({"final_url": "x", "contains": [], "where": "shadow_checked"}, {**state, "url": "x"}), (False, "not_checked"))
        self.assertEqual(runner.judge_final({"final_url": "x", "contains": [], "where": "shadow_checked"}, {**state, "url": "x", "shadow_checked": 1}), (True, "matched"))

    def test_every_real_site_task_is_a_valid_browser_task_with_an_inert_criterion(self):
        from reflexmesh.contracts.browser import BrowserTask

        manifest = json.loads((ROOT / "experiments/v06/m5/manifest.real-sites.json").read_text("utf-8"))
        self.assertEqual(len(manifest["tasks"]), 30)
        for spec in manifest["tasks"]:
            task = BrowserTask.from_dict(runner.task_data(manifest, spec))
            self.assertEqual([c.predicate for c in task.criteria], ["registered_check"])
            self.assertEqual(task.criteria_unsupported(frozenset({runner.EXTERNAL_CHECK})), ())
        check = runner.ExternalOracle()
        check.claim("a")
        self.assertEqual(check.judge({}, [], check.evidence())["status"], "unknown")
        self.assertIsNone(check.settle([], check.evidence(), browser_stopped=True))

    def test_a_claim_of_finishing_is_judged_against_the_oracle(self):
        rows = [{"mode": "J0", "claimed_finish": True, "oracle_success": False, "false_claim": True, "group": "forms", "task": "a"},
                {"mode": "J0", "claimed_finish": True, "oracle_success": True, "false_claim": False, "group": "forms", "task": "b"}]
        s = runner.summarize(rows)["J0"]
        self.assertEqual((s["claimed_finish"], s["false_claim"], s["confirmed_success"]), (2, 1, 1))
        self.assertEqual(s["by_group"], {"forms": "1/2"})

    def test_resuming_keeps_finished_runs_and_repeats_only_infra_failures(self):
        planned = runner.plan({"repeats": 1, "tasks": [{"id": "a"}]})
        done = [{"task": "a", "mode": "J0", "repeat": 1, "category": "ran"}, {"task": "a", "mode": "J1", "repeat": 1, "category": "infra"}]
        kept, todo = runner.remaining(planned, done)
        self.assertEqual([r["mode"] for r in kept], ["J0"])
        self.assertEqual(sorted(r["mode"] for r in todo), ["J1", "L"])

    def test_the_protocol_check_says_it_is_not_the_comparison(self):
        manifest = json.loads((ROOT / "experiments/v06/m5/manifest.protocol-check.json").read_text("utf-8"))
        self.assertEqual(manifest["kind"], "protocol_check")
        self.assertIn("NOT the section 17 comparison", runner.report(manifest, []))


if __name__ == "__main__":
    unittest.main()
