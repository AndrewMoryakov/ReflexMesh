"""Deterministic boundary tests for V0.5 lifecycle, independent of browser packages."""

import copy
import multiprocessing as mp
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.contracts.task import ValidationError
from reflexmesh.adapters.system_one.harness import CountingProvider
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


def unavailable_verifier(task, timeout):
    return [{"id": c.id, "status": "unknown", "evidence_refs": []} for c in task.criteria]


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


class LostResponseEnvironment(FakeEnvironment):
    def __init__(self, count):
        super().__init__()
        self.count = count

    def execute(self, action, params):
        with self.count.get_lock():
            self.count.value += 1
        raise OSError("response lost after effect")


class HungProvider:
    def decide(self, state, questions):
        time.sleep(100)


class InstantProvider:
    def decide(self, state, questions):
        return {"ok": True}


class HungCloseEnvironment(FakeEnvironment):
    def close(self):
        time.sleep(100)


def finish_no_action(gate, events):
    gate.reserve_step()
    return WorkerResult("finish")


def no_confident_action(gate, events):
    gate.reserve_step()
    return WorkerResult("no_confident_action")


def protocol_error(gate, events):
    gate.reserve_step()
    return WorkerResult("protocol_error")


def fail_verifier(task, timeout):
    return [{"id": c.id, "status": "fail", "evidence_refs": ["fake://final"]} for c in task.criteria]


class FakeNavigationEnvironment:
    def __init__(self, task):
        self.task = task

    def reset(self, goal):
        pass

    def observe(self):
        return SimpleNamespace(fields={"url": self.task.origin + self.task.start_path,
                                       "run_id": self.task.run_id, "target_ids": ["form"]})


def bootstrap_then_finish(gate, events, task):
    env = ControlledEnvironment(FakeNavigationEnvironment(task), gate, events, lambda *_: None,
                                bootstrap=True)
    env.reset(task.goal)
    env.observe()
    return WorkerResult("finish")


def consume_steps(gate, events):
    gate.reserve_step()
    gate.reserve_step()
    return WorkerResult("finish")


def consume_calls(gate, events):
    gate.reserve_call()
    gate.reserve_call()
    return WorkerResult("finish")


def adapter_unsupported(gate, events):
    raise RuntimeStop("adapter_contract_unsupported")


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


def submitted_in_flight(gate, events, entered, release):
    env = ControlledEnvironment(FakeEnvironment(entered, release), gate, events, lambda *_: None)
    env.observe()
    env.execute("submit_form", {"element": "submit"})
    return WorkerResult("finish")


def never_returns(gate, events):
    gate.reserve_step()
    gate.reserve_call()
    time.sleep(100)
    return WorkerResult("finish")


def hung_provider(gate, events):
    gate.reserve_step()
    CountingProvider(HungProvider(), gate, events, model=True).decide({}, {})
    return WorkerResult("finish")


def two_micro_decisions(gate, events):
    provider = CountingProvider(InstantProvider(), gate, events, model=True)
    gate.reserve_step()
    provider.decide({}, {})
    provider.decide({}, {})
    return WorkerResult("finish")


def hung_close(gate, events):
    gate.reserve_step()
    ControlledEnvironment(HungCloseEnvironment(), gate, events, lambda *_: None).close()
    return WorkerResult("finish")


def repeat_mutation(gate, events):
    env = ControlledEnvironment(FakeEnvironment(), gate, events, lambda *_: None)
    env.observe()
    env.execute("click", {"element": "submit"})
    env.observe()
    env.execute("click", {"element": "submit"})
    return WorkerResult("finish")


def lost_response_repeat(gate, events, count):
    env = ControlledEnvironment(LostResponseEnvironment(count), gate, events, lambda *_: None)
    env.observe()
    try:
        env.execute("submit_form", {"element": "submit"})
    except OSError:
        pass
    env.observe()
    env.execute("submit_form", {"element": "submit"})
    return WorkerResult("finish")


def pause_during_revalidation(gate, events, entered, release):
    def admit(*_):
        entered.set()
        release.wait(2)
        return None

    env = ControlledEnvironment(FakeEnvironment(), gate, events, admit)
    env.observe()
    env.execute("click", {"element": "submit"})
    return WorkerResult("finish")


def late_budget_corruption(gate, events, entered, release):
    gate.reserve_step()
    entered.set()
    release.wait(2)
    # Fault injection: an executor reports more work than the authorized budget.
    gate.steps.value = gate.max_steps + 1
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

    def test_finish_with_false_postcondition_fails(self):
        result = AttemptSupervisor(self.task(), finish_no_action, fail_verifier).run()
        self.assertEqual((result["attempt_status"], result["task_outcome"], result["stop_reason"]),
                         ("failed", "fail", "postcondition_failed"))

    def test_no_confident_action_can_be_verified(self):
        result = AttemptSupervisor(self.task(), no_confident_action, pass_verifier).run()
        self.assertEqual((result["attempt_status"], result["task_outcome"]), ("completed", "pass"))

    def test_current_page_already_satisfied_after_bootstrap(self):
        data = sample()
        data["criteria"] = [{"id": "page", "kind": "postcondition", "predicate": "current_page",
                             "args": {"path": "/form", "target_id": "form"}}]
        task = ExecutionTask.from_dict(data)
        result = AttemptSupervisor(task, lambda g, e: bootstrap_then_finish(g, e, task), pass_verifier).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("completed", "already_satisfied", "pass"))
        self.assertEqual((len(result["actions"]), result["actions"][0]["effect"]), (1, "applied"))

    def test_protocol_error_with_pass_evidence_stays_failed(self):
        result = AttemptSupervisor(self.task(), protocol_error, pass_verifier).run()
        self.assertEqual((result["attempt_status"], result["task_outcome"], result["verification"][0]["status"]),
                         ("failed", "unknown", "pass"))

    def test_step_and_model_call_limits(self):
        data = sample()
        data["limits"]["max_steps"] = 1
        result = AttemptSupervisor(ExecutionTask.from_dict(data), consume_steps).run()
        self.assertEqual((result["stop_reason"], result["budget"]["steps"]), ("step_limit", 1))
        data["limits"]["max_model_calls"] = 1
        result = AttemptSupervisor(ExecutionTask.from_dict(data), consume_calls).run()
        self.assertEqual((result["stop_reason"], result["budget"]["model_calls"]),
                         ("model_call_limit", 1))

    def test_micro_model_call_limit_before_second_provider_request(self):
        data = sample()
        data["limits"]["max_model_calls"] = 1
        result = AttemptSupervisor(ExecutionTask.from_dict(data), two_micro_decisions).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"],
                          result["budget"]["model_calls"], result["budget"]["dispatches"]),
                         ("incomplete", "model_call_limit", 1, 0))

    def test_unsupported_adapter_contract_blocks(self):
        result = AttemptSupervisor(self.task(), adapter_unsupported).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["budget"]["dispatches"]),
                         ("blocked", "adapter_contract_unsupported", 0))

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

    def test_cancel_after_revalidation_before_commit(self):
        ctx = mp.get_context("fork")
        entered, release = ctx.Event(), ctx.Event()
        sup = AttemptSupervisor(self.task(), lambda g, e: pause_during_revalidation(g, e, entered, release))

        def cancel():
            self.assertTrue(entered.wait(2))
            sup.cancel()
            release.set()

        t = threading.Thread(target=cancel)
        t.start()
        result = sup.run()
        t.join(2)
        self.assertEqual((result["attempt_status"], result["budget"]["dispatches"]), ("cancelled", 0))

    def test_deadline_in_flight_keeps_unknown_effect_and_sequence(self):
        ctx = mp.get_context("fork")
        entered, release = ctx.Event(), ctx.Event()
        sup = AttemptSupervisor(self.task(), lambda g, e: in_flight(g, e, entered, release), pass_verifier)
        t = threading.Timer(0.5, release.set)
        t.start()
        result = sup.run()
        t.join(2)
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "deadline"))
        self.assertEqual(result["budget"]["dispatches"], 1)
        self.assertEqual(result["actions"][0]["effect"], "unknown")
        self.assertLess(result["actions"][0]["proposal_seq"], result["actions"][0]["dispatch_seq"])

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

    def test_cancel_in_flight_late_verification_refines_effect_only(self):
        data = sample()
        data["text_slots"].append({"id": "email", "version": 1, "value": "a@example.test"})
        data["criteria"] = [{"id": "sent", "kind": "postcondition", "predicate": "form_submitted_once",
                             "args": {"name_slot": "name@1", "email_slot": "email@1"}}]
        ctx = mp.get_context("fork")
        entered, release = ctx.Event(), ctx.Event()
        sup = AttemptSupervisor(ExecutionTask.from_dict(data),
                                lambda g, e: submitted_in_flight(g, e, entered, release), pass_verifier)

        def cancel():
            self.assertTrue(entered.wait(2))
            sup.cancel()
            release.set()

        t = threading.Thread(target=cancel)
        t.start()
        result = sup.run()
        t.join(2)
        self.assertEqual((result["attempt_status"], result["task_outcome"], result["actions"][0]["effect"]),
                         ("cancelled", "unknown", "applied"))
        self.assertIsNotNone(result["actions"][0]["return_seq"])

    def test_cancel_in_flight_lost_return_forces_cleanup(self):
        ctx = mp.get_context("fork")
        entered, release = ctx.Event(), ctx.Event()
        sup = AttemptSupervisor(self.task(), lambda g, e: in_flight(g, e, entered, release))

        def cancel():
            self.assertTrue(entered.wait(2))
            sup.cancel()

        t = threading.Thread(target=cancel)
        t.start()
        result = sup.run()
        t.join(2)
        self.assertEqual((result["attempt_status"], result["cleanup"], result["actions"][0]["effect"]),
                         ("cancelled", "forced", "unknown"))
        self.assertIsNone(result["actions"][0]["return_seq"])

    def test_hung_worker_is_bounded(self):
        started = time.monotonic()
        result = AttemptSupervisor(self.task(), never_returns, pass_verifier).run()
        self.assertEqual(result["attempt_status"], "incomplete")
        self.assertEqual(result["stop_reason"], "deadline")
        self.assertEqual(result["cleanup"], "forced")
        self.assertEqual(result["budget"]["model_calls"], 1)
        self.assertLess(time.monotonic() - started, 6.5)

    def test_hung_provider_is_bounded(self):
        result = AttemptSupervisor(self.task(), hung_provider).run()
        self.assertEqual((result["stop_reason"], result["cleanup"], result["budget"]["model_calls"]),
                         ("deadline", "forced", 1))
        self.assertEqual(result["budget"]["dispatches"], 0)

    def test_hung_driver_is_bounded_with_unknown_effect(self):
        ctx = mp.get_context("fork")
        entered, release = ctx.Event(), ctx.Event()
        result = AttemptSupervisor(self.task(), lambda g, e: in_flight(g, e, entered, release)).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["cleanup"],
                          result["budget"]["dispatches"]),
                         ("incomplete", "deadline", "forced", 1))
        self.assertEqual(result["actions"][0]["effect"], "unknown")
        self.assertIsNone(result["actions"][0]["return_seq"])

    def test_hung_close_is_bounded(self):
        result = AttemptSupervisor(self.task(), hung_close).run()
        self.assertEqual((result["stop_reason"], result["cleanup"]), ("deadline", "forced"))

    def test_second_mutating_proposal_is_rejected(self):
        result = AttemptSupervisor(self.task(), repeat_mutation, pass_verifier).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("blocked", "invalid_action"))
        self.assertEqual(result["budget"]["dispatches"], 1)

    def test_lost_submit_response_cannot_repeat(self):
        count = mp.get_context("fork").Value("i", 0)
        result = AttemptSupervisor(self.task(), lambda g, e: lost_response_repeat(g, e, count)).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]),
                         ("incomplete", "effect_unknown"))
        self.assertEqual((count.value, result["budget"]["dispatches"], result["actions"][0]["effect"]),
                         (1, 1, "unknown"))

    def test_hung_verifier_cannot_block_supervisor(self):
        started = time.monotonic()
        result = AttemptSupervisor(self.task(), finish_no_action, hung_verifier).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]),
                         ("incomplete", "deadline"))
        self.assertLess(time.monotonic() - started, 2)

    def test_unavailable_verifier_is_unknown_before_deadline(self):
        result = AttemptSupervisor(self.task(), finish_no_action, unavailable_verifier).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("incomplete", "verification_unknown", "unknown"))

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

    def test_cancel_then_confirmed_late_constraint_violation(self):
        ctx = mp.get_context("fork")
        entered, release = ctx.Event(), ctx.Event()
        sup = AttemptSupervisor(self.task(), lambda g, e: late_budget_corruption(g, e, entered, release))

        def cancel():
            self.assertTrue(entered.wait(2))
            sup.cancel()
            release.set()

        t = threading.Thread(target=cancel)
        t.start()
        result = sup.run()
        t.join(2)
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("cancelled", "cancel_requested", "fail"))
        self.assertEqual(result["verification"][-1]["status"], "fail")


if __name__ == "__main__":
    unittest.main()
