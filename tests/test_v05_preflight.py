"""The fixture preflight and macro call share the supervised deadline and budget."""

import copy
import multiprocessing as mp
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.contracts.task import Route, Task
from reflexmesh.routing.stub import route_task
from reflexmesh.runtime.cli import BrowserExecutionStrategy
from reflexmesh.runtime.runner import AttemptSupervisor
from reflexmesh.adapters.system_one.harness import CountingProvider
from test_v05_runtime import sample


def baseline(task, timeout):
    return {"run_id": task.run_id, "sequence": 0, "state": {}, "log": []}


def selected(task, **kwargs):
    return route_task(Task("0.1", task.task_id, task.goal, (Route.CUA,), (Route.CUA,))).to_dict()


def slow_fixture(task, timeout):
    time.sleep(100)
    return baseline(task, timeout)


class QuickProvider:
    def decide(self, state, questions):
        return {"ok": True}


def micro_after_macro(gate, events):
    CountingProvider(QuickProvider(), gate, events, model=True).decide({}, {})


def fake_harness(*args, **kwargs):
    return micro_after_macro


class PreflightLifecycle(unittest.TestCase):
    def task(self, wall=0.25):
        data = copy.deepcopy(sample())
        data["limits"]["wall_seconds"] = wall
        return ExecutionTask.from_dict(data)

    def args(self):
        return SimpleNamespace(routing_provider="jevrouter", jev_url="http://127.0.0.1:8787",
                               timeout=30, chrome=None)

    def test_macro_request_charged_and_routing_retained(self):
        task = self.task()
        with patch("reflexmesh.runtime.cli.read_fixture", baseline), patch(
                "reflexmesh.runtime.cli.jev_route", selected):
            result = AttemptSupervisor(task, BrowserExecutionStrategy(task, self.args(), None)).run()
        self.assertEqual(result["budget"]["model_calls"], 1)
        self.assertEqual(result["routing"]["route"], "CUA")
        self.assertEqual(result["stop_reason"], "executor_unavailable")
        self.assertEqual(result["budget"]["dispatches"], 0)
        self.assertGreater(result["budget"]["elapsed_seconds"], 0)

    def test_none_route_and_wrong_fixture_block_without_dispatch(self):
        task = self.task()

        def none(task, **kwargs):
            decision = selected(task)
            decision.update(status="abstained", route=None)
            return decision

        with patch("reflexmesh.runtime.cli.read_fixture", baseline), patch(
                "reflexmesh.runtime.cli.jev_route", none):
            result = AttemptSupervisor(task, BrowserExecutionStrategy(task, self.args(), None)).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["budget"]["dispatches"]),
                         ("blocked", "no_route", 0))

        with patch("reflexmesh.runtime.cli.read_fixture", side_effect=ValueError("wrong run")):
            result = AttemptSupervisor(task, BrowserExecutionStrategy(task, self.args(), None)).run()
        self.assertEqual((result["stop_reason"], result["budget"]["model_calls"]),
                         ("fixture_mismatch", 0))

    def test_unknown_predicate_blocks_without_preflight(self):
        data = sample()
        data["criteria"][0]["predicate"] = "unsupported"
        task = ExecutionTask.from_dict(data)
        with patch("reflexmesh.runtime.cli.read_fixture", side_effect=AssertionError("must not read")):
            result = AttemptSupervisor(task, BrowserExecutionStrategy(task, self.args(), None)).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]),
                         ("blocked", "unsupported_criterion"))
        self.assertEqual(result["budget"]["dispatches"], 0)

    def test_macro_call_exhausts_budget_before_micro_request(self):
        data = sample()
        data["limits"]["max_model_calls"] = 1
        task = ExecutionTask.from_dict(data)
        args = self.args()
        args.chrome = sys.executable
        with patch("reflexmesh.runtime.cli.read_fixture", baseline), patch(
                "reflexmesh.runtime.cli.jev_route", selected), patch(
                "reflexmesh.runtime.cli.importlib.util.find_spec", return_value=object()), patch(
                "reflexmesh.runtime.cli.HarnessStrategy", fake_harness):
            result = AttemptSupervisor(task, BrowserExecutionStrategy(task, args, None)).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"],
                          result["budget"]["model_calls"], result["budget"]["dispatches"]),
                         ("incomplete", "model_call_limit", 1, 0))

    def test_cancel_during_macro_cannot_start_execution(self):
        entered, release = mp.get_context("fork").Event(), mp.get_context("fork").Event()

        def pending_route(task, **kwargs):
            entered.set()
            release.wait(1)
            return selected(task)

        task = self.task(1.5)
        sup = AttemptSupervisor(task, BrowserExecutionStrategy(task, self.args(), None))

        def cancel():
            self.assertTrue(entered.wait(1))
            sup.cancel()
            release.set()

        with patch("reflexmesh.runtime.cli.read_fixture", baseline), patch(
                "reflexmesh.runtime.cli.jev_route", pending_route):
            worker = threading.Thread(target=cancel)
            worker.start()
            result = sup.run()
            worker.join(2)
        self.assertEqual((result["attempt_status"], result["stop_reason"]),
                         ("cancelled", "cancel_requested"))
        self.assertEqual(result["budget"]["model_calls"], 1)
        self.assertEqual(result["budget"]["dispatches"], 0)

    def test_hung_fixture_is_bounded_before_macro(self):
        task = self.task(0.1)
        started = time.monotonic()
        with patch("reflexmesh.runtime.cli.read_fixture", slow_fixture), patch(
                "reflexmesh.runtime.cli.jev_route", selected):
            result = AttemptSupervisor(task, BrowserExecutionStrategy(task, self.args(), None)).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "deadline"))
        self.assertEqual(result["budget"]["model_calls"], 0)
        self.assertEqual(result["budget"]["dispatches"], 0)
        self.assertLess(time.monotonic() - started, 6.5)


if __name__ == "__main__":
    unittest.main()
