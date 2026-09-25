"""Pinned SystemOneHarness integration without a browser or live model."""

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.adapters.system_one.harness import HarnessStrategy
from reflexmesh.adapters.system_one.script_selector import FixtureScriptProvider
from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.runner import AttemptSupervisor
from test_v05_runtime import sample, pass_verifier


def order_environment():
    from systemone_harness.envs.order_workflow import OrderWorkflow
    return OrderWorkflow("cancel_fraud")


def order_space():
    from systemone_harness.envs.order_workflow import OrderWorkflow
    return OrderWorkflow.action_space()


def order_provider():
    from systemone_harness.provider import ScriptProvider
    return ScriptProvider(["cancel_order(reason=fraud)", "finish"])


def allow_order(action, params):
    return None if action == "cancel_order" else "policy_denied"


def deny_order(action, params):
    return "policy_denied"


@unittest.skipUnless(importlib.util.find_spec("systemone_harness") and
                     importlib.util.find_spec("httpx"), "pinned optional SystemOneHarness not installed")
class HarnessIntegration(unittest.TestCase):
    def test_script_slot_must_be_offered_exactly(self):
        questions = {"next_action": {"criteria": {"type_text": "Type"}},
                     "type_text__field": {"type": "choice", "criteria": {"0": "[reflex:name]"}},
                     "type_text__value": {"type": "choice", "criteria": {"name@1": "name@1"}}}
        provider = FixtureScriptProvider([{"action": "type_text", "target_id": "name",
                                           "slot": "unknown@1"}])
        with self.assertRaises(ValueError):
            provider.decide({}, questions)
        special = "name,quoted'@1"
        questions["type_text__value"]["criteria"][special] = special
        provider = FixtureScriptProvider([{"action": "type_text", "target_id": "name", "slot": special}])
        self.assertEqual(provider.decide({}, questions).answers["type_text__value"]["choice"], special)

    def test_scripted_harness_through_controlled_environment(self):
        task = ExecutionTask.from_dict(sample())
        strategy = HarnessStrategy("Cancel the fraudulent order", order_environment, order_space,
                                   order_provider, allow_order)
        result = AttemptSupervisor(task, strategy, pass_verifier).run()
        # The test verifier does not attest the order's cancellation effect.
        self.assertEqual(result["attempt_status"], "incomplete", result)
        self.assertEqual(result["stop_reason"], "verification_unknown")
        self.assertEqual(result["budget"]["dispatches"], 1)
        self.assertEqual(result["budget"]["model_calls"], 0)

    def test_policy_veto_prevents_action(self):
        task = ExecutionTask.from_dict(sample())
        strategy = HarnessStrategy("Cancel the fraudulent order", order_environment, order_space,
                                   order_provider, deny_order)
        result = AttemptSupervisor(task, strategy, pass_verifier).run()
        self.assertEqual(result["attempt_status"], "blocked", result)
        self.assertEqual(result["stop_reason"], "policy_denied")
        self.assertEqual(result["budget"]["dispatches"], 0)
