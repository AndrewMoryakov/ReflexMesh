"""Deterministic boundary tests for V0.5 lifecycle, independent of browser packages."""

import copy
import multiprocessing as mp
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.contracts.task import ValidationError
from reflexmesh.runtime.runner import AttemptSupervisor, ControlledEnvironment, RuntimeStop, WorkerResult


def sample():
    return {"schema_version": "execution-task/0.1", "task_id": "form-001", "revision": 1,
            "goal": "Send the form once", "allowed_executors": ["browser.soh"],
            "fixture": {"origin": "http://127.0.0.1:8765", "run_id": "run-1"},
            "start_path": "/form", "permissions": ["navigate", "type_text", "submit_form"],
            "text_slots": [{"id": "name", "version": 1, "value": "secret-text"}],
            "criteria": [{"id": "sent", "kind": "postcondition", "predicate": "account_intact", "args": {}}],
            "limits": {"wall_seconds": 0.3, "max_steps": 3, "max_model_calls": 3,
                       "max_action_retries": 0}}


def pass_verifier(task, timeout):
    return [{"id": c.id, "status": "pass", "evidence_refs": ["fake://verified"]} for c in task.criteria]


def hung_verifier(task, timeout):
    time.sleep(timeout + 10)


def paused_verifier(task, timeout, observation, entered, release):
    entered.set()
    release.wait(2)
    return pass_verifier(task, timeout)


class Result:
    ok = True


class FakeEnvironment:
    def __init__(self, entered=None, release=None):
        self.entered, self.release = entered, release

    def reset(self, goal):
        pass

    def observe(self):
        return {"candidate": "submit"}

    def execute(self, action, params):
        if self.entered:
            self.entered.set()
        if self.release:
            self.release.wait(10)
        return Result()

    def close(self):
        pass


def finish_no_action(gate, events):
    gate.reserve_step()
    return WorkerResult("finish")


def wait_then_dispatch(gate, events, ready, release):
    env = ControlledEnvironment(FakeEnvironment(), gate, events, lambda *_: None)
    env.observe()
    ready.set()
    release.wait(2)
    env.execute("click", {"element": "submit"})
    return WorkerResult("finish")


def in_flight(gate, events, entered, release):
    env = ControlledEnvironment(FakeEnvironment(entered, release), gate, events, lambda *_: None)
    env.observe()
    env.execute("click", {"element": "submit"})
    return WorkerResult("finish")


def never_returns(gate, events):
    gate.reserve_step()
    time.sleep(100)
    return WorkerResult("finish")


def repeat_mutation(gate, events):
    env = ControlledEnvironment(FakeEnvironment(), gate, events, lambda *_: None)
    env.observe()
    env.execute("click", {"element": "submit"})
    env.observe()
    env.execute("click", {"element": "submit"})
    return WorkerResult("finish")


class InputContract(unittest.TestCase):
    def test_valid(self):
        task = ExecutionTask.from_dict(sample())
        self.assertEqual(task.slots[0].reference, "name@1")
        self.assertEqual(task.limits.max_action_retries, 0)

    def test_invalid_inputs(self):
        cases = [
            ("limits", {**sample()["limits"], "wall_seconds": float("nan")}),
            ("limits", {**sample()["limits"], "max_steps": True}),
            ("limits", {**sample()["limits"], "max_action_retries": 1}),
            ("start_path", "/../danger"),
            ("fixture", {"origin": "http://evil.test:8765", "run_id": "run-1"}),
            ("permissions", ["navigate", "navigate"]),
            ("permissions", ["run_shell"]),
            ("text_slots", sample()["text_slots"] * 2),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=str(value)):
                data = copy.deepcopy(sample())
                data[field] = value
                with self.assertRaises(ValidationError):
                    ExecutionTask.from_dict(data)


class Lifecycle(unittest.TestCase):
    def task(self):
        return ExecutionTask.from_dict(sample())

    def test_verified_finish(self):
        result = AttemptSupervisor(self.task(), finish_no_action, pass_verifier).run()
        self.assertEqual((result["attempt_status"], result["task_outcome"]), ("completed", "pass"))
        self.assertEqual(result["budget"]["steps"], 1)
        self.assertEqual(result["actions"], [])

    def test_cancel_before_commit(self):
        ctx = mp.get_context("fork")
        ready, release = ctx.Event(), ctx.Event()
        sup = AttemptSupervisor(self.task(), lambda g, e: wait_then_dispatch(g, e, ready, release), pass_verifier)

        def cancel():
            self.assertTrue(ready.wait(2))
            sup.cancel()
            release.set()

        t = threading.Thread(target=cancel)
        t.start()
        result = sup.run()
        t.join(2)
        self.assertEqual(result["attempt_status"], "cancelled")
        self.assertEqual(result["budget"]["dispatches"], 0)

    def test_cancel_in_flight_retains_unknown_effect(self):
        ctx = mp.get_context("fork")
        entered, release = ctx.Event(), ctx.Event()
        sup = AttemptSupervisor(self.task(), lambda g, e: in_flight(g, e, entered, release), pass_verifier)

        def cancel():
            self.assertTrue(entered.wait(2))
            sup.cancel()
            release.set()

        t = threading.Thread(target=cancel)
        t.start()
        result = sup.run()
        t.join(2)
        self.assertEqual(result["attempt_status"], "cancelled")
        self.assertEqual(result["budget"]["dispatches"], 1)
        self.assertEqual(result["actions"][0]["effect"], "unknown")

    def test_hung_worker_is_bounded(self):
        started = time.monotonic()
        result = AttemptSupervisor(self.task(), never_returns, pass_verifier).run()
        self.assertEqual(result["attempt_status"], "incomplete")
        self.assertEqual(result["stop_reason"], "deadline")
        self.assertLess(time.monotonic() - started, 6.5)

    def test_second_mutating_proposal_is_rejected(self):
        result = AttemptSupervisor(self.task(), repeat_mutation, pass_verifier).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("blocked", "invalid_action"))
        self.assertEqual(result["budget"]["dispatches"], 1)

    def test_hung_verifier_cannot_block_supervisor(self):
        started = time.monotonic()
        result = AttemptSupervisor(self.task(), finish_no_action, hung_verifier).run()
        self.assertNotEqual(result["attempt_status"], "completed")
        self.assertLess(time.monotonic() - started, 2)

    def test_cancel_during_verification_cannot_become_completed(self):
        ctx = mp.get_context("fork")
        entered, release = ctx.Event(), ctx.Event()
        task = self.task()
        task_data = sample()
        task_data["limits"]["wall_seconds"] = 2
        sup = AttemptSupervisor(ExecutionTask.from_dict(task_data), finish_no_action,
                                lambda t, remaining, obs: paused_verifier(t, remaining, obs, entered, release))

        def cancel():
            self.assertTrue(entered.wait(1))
            sup.cancel()
            release.set()

        t = threading.Thread(target=cancel)
        t.start()
        result = sup.run()
        t.join(2)
        self.assertEqual((result["attempt_status"], result["task_outcome"]), ("cancelled", "unknown"))


if __name__ == "__main__":
    unittest.main()
