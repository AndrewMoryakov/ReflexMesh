"""Router usage contracts without network, browser, or live-model execution."""

import json
import multiprocessing as mp
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.routing.router import RouterCapabilities, RouterUsage, RoutingOutcome, StubRouter
from reflexmesh.runtime.cli import BrowserExecutionStrategy
from reflexmesh.runtime.runner import AttemptSupervisor
from test_v05_preflight import baseline, selected
from test_v05_runtime import sample


class LocalTwoStageRouter:
    """Test adapter proves each stage reserves before its provider request."""
    router_id = "test-local-two-stage"
    capabilities = RouterCapabilities("local")

    def __init__(self, calls):
        self.calls = calls

    def route(self, task, *, timeout, reserve_call):
        for _ in range(2):
            reserve_call()
            self.calls.value += 1
        decision = selected(task)
        decision.update(status="abstained", route=None)
        return RoutingOutcome(decision, RouterUsage(2))


class RouterBudgetContract(unittest.TestCase):
    def task(self, calls=1, wall=2):
        data = sample()
        data["limits"].update(max_model_calls=calls, wall_seconds=wall)
        return ExecutionTask.from_dict(data)

    def args(self):
        return SimpleNamespace(routing_provider="jevrouter", jev_url="http://127.0.0.1:8787",
                               timeout=30, chrome=None)

    def run_strategy(self, task, router=None):
        with patch("reflexmesh.runtime.cli.claim_fixture", baseline), patch(
                "reflexmesh.runtime.cli.importlib.util.find_spec", return_value=None):
            return AttemptSupervisor(task, BrowserExecutionStrategy(
                task, self.args(), None, router=router)).run()

    def test_local_multistage_reserves_before_each_actual_call(self):
        for limit, expected_calls, reason in ((1, 1, "model_call_limit"), (2, 2, "no_route"),
                                               (None, 2, "no_route")):
            with self.subTest(limit=limit):
                actual = mp.get_context("fork").Value("i", 0)
                result = self.run_strategy(self.task(limit), LocalTwoStageRouter(actual))
                self.assertEqual(actual.value, expected_calls)
                self.assertEqual(result["budget"]["model_calls"], expected_calls)
                self.assertEqual(result["budget"]["local_model_calls"], expected_calls)
                self.assertEqual(result["stop_reason"], reason)
                self.assertEqual(result["budget"]["dispatches"], 0)
                self.assertEqual(result["router_capabilities"]["model_call_accounting"], "local")

    def test_jev_multistage_is_allowed_and_never_counted_as_one_model_call(self):
        # Reproduces the opaque two-stage case (e.g. CUA+NONE with upstream
        # single_stage_max_candidates=1), without importing/running JevRouter.
        for limit in (1, None):
            with self.subTest(local_limit=limit):
                actual = mp.get_context("fork").Value("i", 0)

                def two_stage(task, **kwargs):
                    actual.value += 2
                    decision = selected(task)
                    decision.update(status="abstained", route=None)
                    return decision

                with patch("reflexmesh.routing.router.jev_route", two_stage):
                    result = self.run_strategy(self.task(limit))
                self.assertEqual(actual.value, 2)
                self.assertEqual(result["stop_reason"], "no_route")
                self.assertIsNone(result["budget"]["model_calls"])
                self.assertEqual(result["budget"]["local_model_calls"], 0)
                self.assertEqual(result["budget"]["remaining_model_calls"], limit)
                self.assertEqual(result["budget"]["router_invocations"], 1)
                self.assertEqual(result["router_usage"], {"model_calls": None})
                self.assertEqual(result["router_capabilities"], {
                    "router_id": "jevrouter", "model_call_accounting": "opaque"})
                assessment = next(row for row in result["verification"] if row["id"] == "runtime.budget")
                self.assertEqual(assessment["scope"], "steps_and_locally_controlled_model_calls")
                json.dumps(result, allow_nan=False)

    def test_router_replacement_does_not_depend_on_cli_provider_branch(self):
        result = self.run_strategy(self.task(), StubRouter())
        self.assertEqual(result["routing"]["provider"], "stub")
        self.assertEqual(result["budget"]["model_calls"], 0)
        self.assertEqual(result["router_usage"], {"model_calls": 0})
        self.assertEqual(result["router_capabilities"]["model_call_accounting"], "none")

    def test_opaque_usage_remains_unknown_on_error_before_response(self):
        with patch("reflexmesh.routing.router.jev_route", side_effect=OSError("lost transport")):
            result = self.run_strategy(self.task())
        self.assertEqual(result["attempt_status"], "failed")
        self.assertIsNone(result["budget"]["model_calls"])
        self.assertEqual(result["budget"]["local_model_calls"], 0)
        self.assertEqual(result["budget"]["model_calls_reason"], "router_internal_usage_unknown")
        self.assertIsNone(result["router_usage"])

    def test_opaque_usage_persists_when_deadline_kills_router_worker(self):
        def never_returns(task, **kwargs):
            time.sleep(100)

        with patch("reflexmesh.routing.router.jev_route", never_returns), patch(
                "reflexmesh.runtime.runner.GRACE_SECONDS", 0.05):
            result = self.run_strategy(self.task(wall=0.2))
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "deadline"))
        self.assertIsNone(result["budget"]["model_calls"])
        self.assertEqual(result["budget"]["local_model_calls"], 0)
        self.assertIsNone(result["router_usage"])
        self.assertEqual(result["budget"]["dispatches"], 0)


if __name__ == "__main__":
    unittest.main()
