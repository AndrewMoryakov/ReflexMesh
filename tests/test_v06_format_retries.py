"""Experiment M5-H: asking a model again once after a malformed reply, and only then.

No model and no browser. The default of zero retries must leave the behaviour of the earlier comparison untouched.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SOH = importlib.util.find_spec("systemone_harness") is not None
spec = importlib.util.spec_from_file_location("m5_runner", ROOT / "experiments/v06/m5/runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def refusal(message="protocol: next_action names an operation that was not offered", usage=None):
    from systemone_harness.provider import ProviderError

    exc = ProviderError(message)
    exc.request_id, exc.usage = "r", usage
    return exc


class Inner:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def decide(self, state, questions):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@unittest.skipUnless(SOH, "SystemOneHarness is not installed")
class FormatRetryProvider(unittest.TestCase):
    def provider(self, outcomes, retries):
        from reflexmesh.runtime.browser_run import FormatRetryProvider

        inner = Inner(outcomes)
        return FormatRetryProvider(inner, retries), inner

    def test_zero_retries_is_the_provider_unchanged(self):
        provider, inner = self.provider([refusal(), "second"], 0)
        with self.assertRaises(Exception):
            provider.decide({}, {})
        self.assertEqual(inner.calls, 1)

    def test_a_malformed_reply_is_asked_again_once_and_a_second_one_ends_the_attempt(self):
        provider, inner = self.provider([refusal(), "answer"], 1)
        self.assertEqual(provider.decide({}, {}), "answer")
        self.assertEqual(inner.calls, 2)
        provider, inner = self.provider([refusal(), refusal(), "never reached"], 1)
        with self.assertRaises(Exception) as caught:
            provider.decide({}, {})
        self.assertTrue(str(caught.exception).startswith("protocol:"))
        self.assertEqual(inner.calls, 2)

    def test_only_a_malformed_reply_is_asked_again(self):
        for failure in (refusal("HTTP 500"), refusal("timeout"), RuntimeError("the budget is spent")):
            provider, inner = self.provider([failure, "answer"], 1)
            with self.assertRaises(Exception):
                provider.decide({}, {})
            self.assertEqual(inner.calls, 1, failure)

    def test_the_attributes_the_controller_reads_come_from_the_inner_provider(self):
        from reflexmesh.runtime.browser_run import FormatRetryProvider

        inner = Inner([])
        inner.model, inner._client = "m", object()
        wrapped = FormatRetryProvider(inner, 1)
        self.assertEqual((wrapped.model, wrapped._client), ("m", inner._client))


class Core:
    def __init__(self, reservations):
        self.reservations = reservations
        self.reserved = 0

    def reserve_assist_call(self, attempt_id):
        self.reserved += 1
        return self.reserved <= self.reservations

    def resume(self, request):
        return {"schema_version": "browser-result/0.1", "request": request}


class Client:
    def __init__(self, outcomes):
        self.outcomes, self.asked = list(outcomes), 0

    def ask(self, system, payload):
        self.asked += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, {"input_tokens": 10, "output_tokens": 2, "cost": 0.5}, "m", "id", 1.0


PROGRESS = {"schema_version": "browser-progress/0.1", "attempt_id": "a", "assistance_steps_remaining": 1,
            "continuation_token": "t", "observation": {"observation_id": 7, "page": {}, "text": ""},
            "action_options": [{"ref": "7:elements:3", "operation": "click", "name": "Go"}]}
GOOD = {"option_ref": "7:elements:3"}


@unittest.skipUnless(SOH, "SystemOneHarness is not installed")
class AssistantRetry(unittest.TestCase):
    def run_help(self, outcomes, retries, reservations=10):
        client, core = Client(outcomes), Core(reservations)
        assistant = runner.Assistant(client, "goal", retries)
        result = assistant.help(core, dict(PROGRESS))
        return assistant, client, core, result

    def test_without_retries_a_reply_naming_nothing_offered_ends_the_help(self):
        assistant, client, core, result = self.run_help([{"option_ref": "nope"}, GOOD], 0)
        self.assertEqual((client.asked, assistant.retried), (1, 0))
        self.assertEqual(assistant.steps, 0)

    def test_with_one_retry_the_second_reply_is_used_and_both_are_paid_and_counted(self):
        assistant, client, core, result = self.run_help([{"option_ref": "nope"}, GOOD], 1)
        self.assertEqual((client.asked, assistant.calls, assistant.retried, core.reserved), (2, 2, 1, 2))
        self.assertEqual(assistant.cost, 1.0)
        self.assertEqual(result["request"]["proposal"]["option_ref"], "7:elements:3")

    def test_an_unreadable_reply_that_was_paid_for_is_asked_again(self):
        usage = {"input_tokens": 10, "output_tokens": 2, "cost": 0.25}
        assistant, client, core, result = self.run_help([refusal("protocol: not JSON", usage), GOOD], 1)
        self.assertEqual((client.asked, assistant.retried), (2, 1))
        self.assertEqual(assistant.cost, 0.75)

    def test_a_failed_request_with_no_reply_and_a_spent_budget_are_not_asked_again(self):
        assistant, client, core, result = self.run_help([refusal("HTTP 500", None), GOOD], 1)
        self.assertEqual((client.asked, assistant.retried), (1, 0))
        assistant, client, core, result = self.run_help([{"option_ref": "nope"}, GOOD], 1, reservations=1)
        self.assertEqual((client.asked, core.reserved), (1, 2))   # the retry asked the counter and was refused

    def test_done_is_not_a_malformed_reply(self):
        assistant, client, core, result = self.run_help([{"done": True}, GOOD], 1)
        self.assertEqual((client.asked, assistant.retried), (1, 0))

    def test_the_second_malformed_reply_ends_the_help_too(self):
        assistant, client, core, result = self.run_help([{"option_ref": "x"}, {"option_ref": "y"}, GOOD], 1)
        self.assertEqual((client.asked, assistant.retried), (2, 1))


class Arms(unittest.TestCase):
    def test_a_manifest_names_its_arms_and_every_arm_runs_for_every_task_and_repeat(self):
        manifest = {"repeats": 3, "tasks": [{"id": "a"}, {"id": "b"}], "modes": ["J0", "J1", "J1R", "LR"]}
        runs = runner.plan(manifest)
        self.assertEqual(len(runs), 3 * 2 * 4)
        for repeat in (1, 2, 3):
            self.assertEqual({r["mode"] for r in runs if r["task"]["id"] == "a" and r["repeat"] == repeat},
                             {"J0", "J1", "J1R", "LR"})
        firsts = [next(r["mode"] for r in runs if r["repeat"] == n) for n in (1, 2, 3)]
        self.assertEqual(len(set(firsts)), 3)

    def test_without_modes_the_three_of_the_earlier_comparison_are_planned(self):
        runs = runner.plan({"repeats": 3, "tasks": [{"id": "a"}]})
        self.assertEqual({r["mode"] for r in runs}, set(runner.MODES))

    def test_a_summary_lists_the_arms_that_ran(self):
        rows = [{"mode": mode, "task": "a", "repeat": 1, "oracle_success": True} for mode in ("J0", "J1R", "LR")]
        self.assertEqual(list(runner.summarize(rows)), ["J0", "J1R", "LR"])


if __name__ == "__main__":
    unittest.main()
