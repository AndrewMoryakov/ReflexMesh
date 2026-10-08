"""Optional-budget regressions with no browser, installed harness, or live model.

The deliberately blocked subprocesses have an independent test watchdog. An
unlimited runtime regression must fail the test rather than hang the test run.
"""

import copy
import io
import itertools
import json
import math
import multiprocessing as mp
import os
import queue
import signal
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.adapters.system_one.harness import CountingProvider, HarnessStrategy
from reflexmesh.cli import main
from reflexmesh.contracts.execution import ExecutionTask, Limits
from reflexmesh.contracts.task import ValidationError
from reflexmesh.runtime.cli import BrowserExecutionStrategy, run_execution
from reflexmesh.runtime.ownership import FixtureOwnership
from reflexmesh.runtime.runner import (
    GRACE_SECONDS,
    KILL_SECONDS,
    AttemptGate,
    AttemptSupervisor,
    ControlledEnvironment,
    RuntimeStop,
    WorkerResult,
)
from reflexmesh.verification.verifier import FixtureVerifier, read_fixture
from test_v05_preflight import baseline as claim_receipt


def task_data():
    return {
        "schema_version": "execution-task/0.1",
        "task_id": "optional-budgets",
        "revision": 1,
        "goal": "Send the form once",
        "allowed_executors": ["browser.soh"],
        "fixture": {"origin": "http://127.0.0.1:8765", "run_id": "optional-1"},
        "start_path": "/form",
        "permissions": ["navigate", "type_text", "submit_form"],
        "text_slots": [{"id": "name", "version": 1, "value": "test-name"}],
        "criteria": [{"id": "intact", "kind": "postcondition",
                      "predicate": "account_intact", "args": {}}],
        "limits": {"wall_seconds": 30, "max_steps": 2, "max_model_calls": 2,
                   "max_action_retries": 0},
    }


def make_task(*, wall=None, steps=None, calls=None):
    data = task_data()
    data["limits"].update(wall_seconds=wall, max_steps=steps, max_model_calls=calls)
    return ExecutionTask.from_dict(data)


def pass_verifier(task, timeout):
    return [{"id": criterion.id, "status": "pass", "evidence_refs": ["fake://verified"]}
            for criterion in task.criteria]


def finish_one(gate, events):
    gate.reserve_step()
    return WorkerResult("finish")


def consume_two_each(gate, events):
    for _ in range(2):
        gate.reserve_step()
        gate.reserve_call()
    return WorkerResult("finish")


class QuickEnvironment:
    def __init__(self):
        self.closed = False

    def observe(self):
        return SimpleNamespace(fields={})

    def execute(self, action, params):
        return SimpleNamespace(ok=True)

    def close(self):
        self.closed = True


class QuickProvider:
    def __init__(self):
        self._client = SimpleNamespace(timeout=None)
        self.closed = False

    def decide(self, state, questions):
        return {"ok": True}

    def close(self):
        self.closed = True


def submit_then_finish(gate, events):
    env = ControlledEnvironment(QuickEnvironment(), gate, events, lambda *_: None)
    env.observe()
    env.execute("submit_form", {"element": "send-form"})
    return WorkerResult("finish")


@contextmanager
def fake_harness_modules(controller=None):
    """Only fake modules can be imported by the optional adapter in these tests."""
    package = ModuleType("systemone_harness")
    package.__path__ = []
    envs = ModuleType("systemone_harness.envs")
    envs.__path__ = []
    browser = ModuleType("systemone_harness.envs.browser")
    browser.default_chrome = lambda: sys.executable
    controller_module = ModuleType("systemone_harness.controller")
    controller_module.Controller = controller
    package.envs = envs
    package.controller = controller_module
    envs.browser = browser
    with patch.dict(sys.modules, {
        "systemone_harness": package,
        "systemone_harness.envs": envs,
        "systemone_harness.envs.browser": browser,
        "systemone_harness.controller": controller_module,
    }):
        yield


@contextmanager
def finite_watchdog(seconds):
    """Interrupt a broken unlimited wait independently of the attempt's gate."""
    if threading.current_thread() is not threading.main_thread():
        raise unittest.SkipTest("SIGALRM watchdog requires the main test thread")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def timed_out(*_):
        raise AssertionError("optional-budget test exceeded its finite watchdog")

    signal.signal(signal.SIGALRM, timed_out)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            remaining = max(0.001, previous_timer[0] - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


class OptionalLimitContract(unittest.TestCase):
    def test_each_budget_can_be_disabled_independently(self):
        for wall, steps, calls in itertools.product((30, None), (2, None), (2, None)):
            with self.subTest(wall=wall, steps=steps, calls=calls):
                task = make_task(wall=wall, steps=steps, calls=calls)
                self.assertEqual(task.limits, Limits(wall, steps, calls, 0))

    def test_whole_limits_null_is_the_explicit_all_unlimited_shorthand(self):
        data = task_data()
        data["limits"] = None
        self.assertEqual(ExecutionTask.from_dict(data).limits, Limits(None, None, None, 0))

    def test_zero_negative_boolean_and_nonfinite_are_not_unlimited_aliases(self):
        for field in ("wall_seconds", "max_steps", "max_model_calls"):
            for value in (0, -1, True, False, math.inf, -math.inf, math.nan, "unlimited"):
                with self.subTest(field=field, value=value):
                    data = task_data()
                    data["limits"][field] = value
                    with self.assertRaises(ValidationError):
                        ExecutionTask.from_dict(data)

    def test_count_limits_still_require_integers(self):
        for field in ("max_steps", "max_model_calls"):
            for value in (1.5, 2.0):
                with self.subTest(field=field, value=value):
                    data = task_data()
                    data["limits"][field] = value
                    with self.assertRaises(ValidationError):
                        ExecutionTask.from_dict(data)

    def test_disabling_budgets_does_not_enable_automatic_retries(self):
        for retries in (None, True, -1, 1, math.inf):
            with self.subTest(retries=retries):
                data = task_data()
                data["limits"] = {"wall_seconds": None, "max_steps": None,
                                  "max_model_calls": None, "max_action_retries": retries}
                with self.assertRaises(ValidationError):
                    ExecutionTask.from_dict(data)

    def test_limits_are_still_required_and_objects_have_exact_fields(self):
        data = task_data()
        del data["limits"]
        with self.assertRaises(ValidationError):
            ExecutionTask.from_dict(data)
        for limits in ({}, {"wall_seconds": None}, {**task_data()["limits"], "extra": None},
                       False, [], "none"):
            with self.subTest(limits=limits):
                data = task_data()
                data["limits"] = limits
                with self.assertRaises(ValidationError):
                    ExecutionTask.from_dict(data)


@unittest.skipUnless("fork" in mp.get_all_start_methods(), "requires fork shared state")
class OptionalGateLimits(unittest.TestCase):
    def gate(self, **limits):
        return AttemptGate(mp.get_context("fork"), make_task(**limits), started=1000.0)

    def test_unlimited_gate_still_admits_work_after_an_hour(self):
        gate = self.gate()
        with patch("reflexmesh.runtime.runner.time", SimpleNamespace(monotonic=lambda: 4601.0)):
            self.assertIsNone(gate.deadline)
            self.assertIsNone(gate.remaining())
            self.assertIsNone(gate.wall_remaining())
            self.assertFalse(gate.deadline_expired())
            for _ in range(100):
                gate.reserve_step()
                gate.reserve_call()
            self.assertEqual(gate.operation_timeout(2.0), 2.0)
            self.assertEqual(gate.operation_timeout(30.0), 30.0)
            self.assertEqual(gate.operation_timeout(120.0), 120.0)
        self.assertEqual((gate.steps.value, gate.calls.value), (100, 100))

    def test_finite_operation_timeout_uses_remaining_wall_budget(self):
        gate = self.gate(wall=10)
        with patch("reflexmesh.runtime.runner.time", SimpleNamespace(monotonic=lambda: 1008.5)):
            self.assertEqual(gate.remaining(), 1.5)
            self.assertEqual(gate.wall_remaining(), 1.5)
            self.assertEqual(gate.operation_timeout(30), 1.5)
            self.assertEqual(gate.operation_timeout(0.5), 0.5)
        self.assertEqual(gate.wall_remaining(now=1011.0), 0.0)

    def test_expired_wall_budget_stops_even_when_counts_are_unlimited(self):
        for method in ("remaining", "reserve_step", "reserve_call"):
            with self.subTest(method=method):
                gate = self.gate(wall=10)
                with patch("reflexmesh.runtime.runner.time", SimpleNamespace(monotonic=lambda: 1010.0)):
                    with self.assertRaises(RuntimeStop) as stopped:
                        getattr(gate, method)()
                self.assertEqual(stopped.exception.reason, "deadline")
                self.assertEqual(gate.wall_remaining(now=1010.0), 0.0)

    def test_finite_count_stops_while_other_dimensions_are_unlimited(self):
        for limited, reserve, unlimited, reason in (
            ("steps", "reserve_step", "reserve_call", "step_limit"),
            ("calls", "reserve_call", "reserve_step", "model_call_limit"),
        ):
            with self.subTest(limited=limited):
                gate = self.gate(**{limited: 1})
                for _ in range(100):
                    getattr(gate, unlimited)()
                getattr(gate, reserve)()
                with self.assertRaises(RuntimeStop) as stopped:
                    getattr(gate, reserve)()
                self.assertEqual(stopped.exception.reason, reason)
                self.assertEqual(getattr(gate, limited).value, 1)
                self.assertIsNone(gate.wall_remaining())

    def test_cancel_still_wins_and_rejects_later_work_without_deadline(self):
        gate = self.gate()
        self.assertTrue(gate.stop("cancel_requested"))
        self.assertFalse(gate.stop("cancel_requested"))
        for method in ("remaining", "reserve_step", "reserve_call", "commit_dispatch"):
            with self.subTest(method=method), self.assertRaises(RuntimeStop) as stopped:
                getattr(gate, method)()
            self.assertEqual(stopped.exception.reason, "cancel_requested")
        self.assertEqual(gate.finish(), 1)
        self.assertIsNone(gate.wall_remaining())

    def test_harness_receives_internal_infinity_and_no_independent_timer(self):
        for wall, steps in ((None, None), (30, None), (None, 4), (30, 4)):
            with self.subTest(wall=wall, steps=steps):
                task = make_task(wall=wall, steps=steps)
                gate = AttemptGate(mp.get_context("fork"), task, time.monotonic())
                environment, provider, space = QuickEnvironment(), QuickProvider(), object()
                controller = Mock()
                controller.return_value.run.return_value = SimpleNamespace(status="completed", reason="finish")
                strategy = HarnessStrategy(task.goal, lambda: environment, lambda: space,
                                           lambda: provider, lambda *_: None)
                with fake_harness_modules(controller):
                    result = strategy(gate, queue.Queue())
                kwargs = controller.call_args.kwargs
                self.assertEqual(kwargs["max_steps"], math.inf if steps is None else steps)
                self.assertIsNone(kwargs["timeout_seconds"])
                self.assertEqual(result.status, "finish")
                self.assertTrue(environment.closed)
                self.assertTrue(provider.closed)

    def test_preflight_and_execution_have_no_hidden_sixty_second_cap(self):
        now = [1000.0]
        task = make_task()
        context = mp.get_context("fork")
        owner = FixtureOwnership(context, task, "unlimited-preflight")
        gate = AttemptGate(context, task, now[0], fixture_owner=owner)
        events = queue.Queue()
        args = SimpleNamespace(routing_provider="stub", chrome=sys.executable, timeout=30.0,
                               jev_url="http://127.0.0.1:8787")

        def slow_preflight(task, timeout, owner=None):
            self.assertEqual(timeout, 2.0)
            now[0] += 3601.0
            return claim_receipt(task, timeout, owner)

        def later_execution(gate, events):
            now[0] += 3601.0
            self.assertIsNone(gate.remaining())
            return finish_one(gate, events)

        with (fake_harness_modules(),
              patch("reflexmesh.runtime.runner.time", SimpleNamespace(monotonic=lambda: now[0])),
              patch("reflexmesh.runtime.cli.claim_fixture", side_effect=slow_preflight) as read,
              patch("reflexmesh.runtime.cli.importlib.util.find_spec", return_value=object()),
              patch("reflexmesh.runtime.cli.HarnessStrategy", return_value=later_execution) as harness):
            result = BrowserExecutionStrategy(task, args, [])(gate, events)
        self.assertEqual(result.status, "finish")
        self.assertEqual(now[0] - gate.started, 7202.0)
        read.assert_called_once()
        harness.assert_called_once()
        self.assertEqual(gate.steps.value, 1)

    def test_finite_preflight_time_still_counts_against_task_deadline(self):
        now = [1000.0]
        task = make_task(wall=3600)
        context = mp.get_context("fork")
        owner = FixtureOwnership(context, task, "finite-preflight")
        gate = AttemptGate(context, task, now[0], fixture_owner=owner)
        args = SimpleNamespace(routing_provider="stub", chrome=sys.executable, timeout=30.0,
                               jev_url="http://127.0.0.1:8787")

        def slow_preflight(task, timeout, owner=None):
            now[0] += 3601.0
            return claim_receipt(task, timeout, owner)

        with (patch("reflexmesh.runtime.runner.time", SimpleNamespace(monotonic=lambda: now[0])),
              patch("reflexmesh.runtime.cli.claim_fixture", side_effect=slow_preflight),
              patch("reflexmesh.runtime.cli.HarnessStrategy") as harness):
            with self.assertRaises(RuntimeStop) as stopped:
                BrowserExecutionStrategy(task, args, [])(gate, queue.Queue())
        self.assertEqual(stopped.exception.reason, "deadline")
        harness.assert_not_called()

    def test_model_io_timeout_is_finite_without_task_deadline(self):
        gate = self.gate()
        provider = QuickProvider()
        counted = CountingProvider(provider, gate, queue.Queue(), model=True)
        with patch("reflexmesh.runtime.runner.time", SimpleNamespace(monotonic=lambda: 4601.0)):
            for _ in range(4):
                self.assertEqual(counted.decide({}, {}), {"ok": True})
                self.assertEqual(provider._client.timeout, 30.0)
                self.assertTrue(math.isfinite(provider._client.timeout))
        self.assertEqual(gate.calls.value, 4)


LINUX_WATCHDOG = (sys.platform.startswith("linux") and hasattr(os, "WNOWAIT") and
                  hasattr(signal, "setitimer") and "fork" in mp.get_all_start_methods())


@unittest.skipUnless(LINUX_WATCHDOG, "owned-worker lifecycle and watchdog require Linux")
class OptionalBudgetLifecycle(unittest.TestCase):
    def run_bounded(self, supervisor, *, watchdog=12.0):
        processes = []
        make_process = supervisor.context.Process

        def record_process(*args, **kwargs):
            process = make_process(*args, **kwargs)
            processes.append(process)
            return process

        try:
            with (patch.object(supervisor.context, "Process", new=record_process),
                  finite_watchdog(watchdog)):
                result = supervisor.run()
            self.assertFalse(any(process.is_alive() for process in processes),
                             "supervisor leaked a verifier process")
            self.assertFalse(supervisor.process.is_alive(), "supervisor leaked its owned worker")
            # No Infinity/NaN sentinel may escape into any result or trace field.
            json.dumps(result, allow_nan=False)
            return result
        finally:
            for process in processes:
                if process.pid is not None:
                    if process.is_alive():
                        process.kill()
                    process.join(timeout=1.0)
                    if not process.is_alive():
                        process.close()
            if supervisor.process.pid is not None and not supervisor.process._released:
                supervisor.process.cleanup(1.0)
            supervisor.events.close()

    def test_dead_verifier_final_queue_drain_preserves_racing_result(self):
        for published in (False, True):
            with self.subTest(published=published):
                supervisor = AttemptSupervisor(make_task(), finish_one, pass_verifier)
                results, process = Mock(), Mock()
                results.get.side_effect = queue.Empty
                if published:
                    results.get_nowait.return_value = pass_verifier(supervisor.task, None)
                else:
                    results.get_nowait.side_effect = queue.Empty
                process.is_alive.return_value = False
                try:
                    with (patch.object(supervisor.context, "Queue", return_value=results),
                          patch.object(supervisor.context, "Process", return_value=process) as constructor,
                          finite_watchdog(2.0)):
                        verification = supervisor._verify(None, interruptible=True)
                finally:
                    supervisor.events.close()
                self.assertEqual(verification[0]["status"], "pass" if published else "unknown")
                self.assertEqual(verification[0]["evidence_refs"], ["fake://verified"] if published else [])
                results.get.assert_called_once()
                results.get_nowait.assert_called_once()
                results.close.assert_called_once()
                process.start.assert_called_once()
                process.join.assert_called_once()
                self.assertTrue(math.isfinite(process.join.call_args.kwargs["timeout"]))
                self.assertIsNone(constructor.call_args.kwargs["args"][2])

    def test_mixed_results_preserve_null_and_numeric_exhaustion(self):
        for wall, steps, calls in itertools.product((30, None), (2, None), (2, None)):
            with self.subTest(wall=wall, steps=steps, calls=calls):
                task = make_task(wall=wall, steps=steps, calls=calls)
                timeout_seen = mp.get_context("fork").Event()

                def verify(task, timeout):
                    if ((wall is None and timeout is None) or
                            (wall is not None and timeout is not None and 0 < timeout <= wall)):
                        timeout_seen.set()
                    return pass_verifier(task, timeout)

                result = self.run_bounded(AttemptSupervisor(task, consume_two_each, verify))
                self.assertEqual((result["attempt_status"], result["task_outcome"]), ("incomplete", "unknown"))
                self.assertTrue(timeout_seen.is_set())
                budget = result["budget"]
                self.assertEqual(budget["limits"], {"wall_seconds": wall, "max_steps": steps,
                                                    "max_model_calls": calls})
                self.assertEqual((budget["steps"], budget["model_calls"]), (2, 2))
                self.assertEqual(budget["remaining_steps"], None if steps is None else 0)
                self.assertEqual(budget["remaining_model_calls"], None if calls is None else 0)
                if wall is None:
                    self.assertIsNone(budget["remaining_wall_seconds"])
                else:
                    self.assertGreater(budget["remaining_wall_seconds"], 0)
                    self.assertLessEqual(budget["remaining_wall_seconds"], wall)
                self.assertEqual(result["verification"][-1]["status"], "pass")

    def test_unlimited_result_can_record_more_than_an_hour_elapsed(self):
        supervisor = AttemptSupervisor(make_task(), finish_one, pass_verifier)
        supervisor.started -= 3601.0
        supervisor.gate.started = supervisor.started
        result = self.run_bounded(supervisor)
        self.assertEqual((result["attempt_status"], result["stop_reason"]),
                         ("incomplete", "verification_unknown"))
        self.assertGreaterEqual(result["budget"]["elapsed_seconds"], 3601.0)
        self.assertIsNone(result["budget"]["remaining_wall_seconds"])

    def test_finite_count_stop_is_preserved_in_mixed_unlimited_result(self):
        for task, reason, steps, calls in (
            (make_task(steps=1), "step_limit", 1, 1),
            (make_task(calls=1), "model_call_limit", 2, 1),
        ):
            with self.subTest(reason=reason):
                result = self.run_bounded(AttemptSupervisor(task, consume_two_each))
                self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", reason))
                self.assertEqual((result["budget"]["steps"], result["budget"]["model_calls"]), (steps, calls))
                self.assertIsNone(result["budget"]["remaining_wall_seconds"])
                self.assertEqual(result["verification"][-1]["status"], "pass")

    def test_exhausted_wall_result_uses_zero_with_unlimited_count_budgets(self):
        supervisor = AttemptSupervisor(make_task(wall=30), finish_one)
        supervisor.gate.deadline = time.monotonic() - 1.0
        result = self.run_bounded(supervisor)
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "deadline"))
        self.assertEqual(result["budget"]["remaining_wall_seconds"], 0.0)
        self.assertIsNone(result["budget"]["remaining_steps"])
        self.assertIsNone(result["budget"]["remaining_model_calls"])
        self.assertEqual(result["budget"]["limits"], {
            "wall_seconds": 30, "max_steps": None, "max_model_calls": None,
        })

    def test_cancel_unlimited_hung_worker_preserves_bounded_forced_cleanup(self):
        for ignore_term in (False, True):
            with self.subTest(ignore_term=ignore_term):
                entered = mp.get_context("fork").Event()

                def hung_worker(gate, events):
                    if ignore_term:
                        signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    gate.reserve_step()
                    gate.reserve_call()
                    entered.set()
                    threading.Event().wait()

                supervisor = AttemptSupervisor(make_task(), hung_worker)
                cancellation = []

                def cancel():
                    ready = entered.wait(2.0)
                    when = time.monotonic()
                    cancellation.append((ready, supervisor.cancel(), when))

                thread = threading.Thread(target=cancel, daemon=True)
                thread.start()
                try:
                    result = self.run_bounded(supervisor)
                    finished = time.monotonic()
                finally:
                    thread.join(timeout=2.0)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(cancellation), 1)
                ready, accepted, cancelled_at = cancellation[0]
                self.assertTrue(ready)
                self.assertTrue(accepted)
                self.assertLess(finished - cancelled_at, GRACE_SECONDS + KILL_SECONDS + 1.0)
                self.assertEqual((result["attempt_status"], result["stop_reason"], result["cleanup"]),
                                 ("cancelled", "cancel_requested", "forced"))
                self.assertEqual((result["budget"]["steps"], result["budget"]["model_calls"]), (1, 1))
                self.assertEqual(result["task_outcome"], "unknown")

    def cancel_blocked_verifier(self, *, ignore_term=False, late_pass=False):
        ctx = mp.get_context("fork")
        entered, timeout_none, returned = ctx.Event(), ctx.Event(), ctx.Event()
        data = task_data()
        data["limits"] = None
        data["text_slots"].append({"id": "email", "version": 1, "value": "test@example.test"})
        data["criteria"] = [{"id": "sent", "kind": "postcondition", "predicate": "form_submitted_once",
                             "args": {"name_slot": "name@1", "email_slot": "email@1"}}]

        def verifier(task, timeout):
            stopped = False

            def stop(*_):
                nonlocal stopped
                stopped = True

            if late_pass:
                signal.signal(signal.SIGTERM, stop)
            elif ignore_term:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if timeout is None:
                timeout_none.set()
            entered.set()
            if not late_pass:
                threading.Event().wait()
            while not stopped:
                time.sleep(0.01)
            returned.set()
            return pass_verifier(task, timeout)

        supervisor = AttemptSupervisor(ExecutionTask.from_dict(data), submit_then_finish, verifier)
        cancellation = []

        def cancel():
            ready = entered.wait(2.0)
            when = time.monotonic()
            cancellation.append((ready, supervisor.cancel(), when))

        thread = threading.Thread(target=cancel, daemon=True)
        thread.start()
        try:
            result = self.run_bounded(supervisor, watchdog=8.0)
            finished = time.monotonic()
        finally:
            thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(cancellation), 1)
        ready, accepted, cancelled_at = cancellation[0]
        self.assertTrue(ready)
        self.assertTrue(accepted)
        self.assertTrue(timeout_none.is_set())
        self.assertLess(finished - cancelled_at, 2.0)
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("cancelled", "cancel_requested", "unknown"))
        self.assertEqual(result["cleanup"], "closed")
        self.assertEqual(result["verification"][0]["status"], "unknown")
        self.assertEqual(result["verification"][0]["evidence_refs"], [])
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["effect"], "unknown")
        self.assertEqual(result["actions"][0]["evidence_refs"], [])
        self.assertIsNotNone(result["actions"][0]["return_seq"])
        self.assertEqual(returned.is_set(), late_pass)

    def test_cancel_unlimited_verifier_interrupts_both_term_and_kill_cases(self):
        for ignore_term in (False, True):
            with self.subTest(ignore_term=ignore_term):
                self.cancel_blocked_verifier(ignore_term=ignore_term)

    def test_late_pass_on_sigterm_cannot_complete_or_refine_cancelled_attempt(self):
        self.cancel_blocked_verifier(late_pass=True)


class OptionalVerificationTimeout(unittest.TestCase):
    def test_unlimited_fixture_read_still_has_a_finite_network_timeout(self):
        task = make_task()
        payload = {"run_id": task.run_id, "state": {}, "log": []}
        for remaining, expected in ((None, 2.0), (3600, 2.0), (0.5, 0.5)):
            with self.subTest(remaining=remaining):
                with patch("reflexmesh.verification.verifier.urllib.request.urlopen",
                           return_value=io.StringIO(json.dumps(payload))) as urlopen:
                    self.assertEqual(read_fixture(task, remaining), payload)
                self.assertEqual(urlopen.call_args.kwargs["timeout"], expected)
                self.assertTrue(math.isfinite(urlopen.call_args.kwargs["timeout"]))

    def test_fixture_verifier_accepts_none_without_arithmetic_or_timeout_sentinel(self):
        task = make_task()
        owner = FixtureOwnership(mp.get_context("fork"), task, "unlimited-verifier")
        receipt = claim_receipt(task, None, owner)
        baseline = receipt["baseline"]
        current = {**baseline, "sequence": 1, "state": {"account_deleted": False},
                   "binding": {**receipt["binding"], "session_id": "test-session",
                               "profile_id": "test-profile"},
                   "baseline": baseline, "phase": "active", "effects": [], "pending_effects": []}
        # Timeout forwarding seam only; the actual client authentication and
        # fixture attribution are exercised by separate real-HTTP regressions.
        with patch.object(owner, "read", return_value=current) as read:
            result = FixtureVerifier(task, baseline, ownership=owner)(task, None)
        self.assertEqual(result[0]["status"], "pass")
        self.assertIsNone(read.call_args.args[1] if len(read.call_args.args) > 1
                          else read.call_args.kwargs["timeout"])
        json.dumps(result, allow_nan=False)


class NoLimitsCli(unittest.TestCase):
    def args(self, output, *, no_limits):
        return SimpleNamespace(input="task.json", output_dir=str(output), no_limits=no_limits,
                               routing_provider="stub", action_provider="jev", script=None,
                               chrome=None, jev_url="http://127.0.0.1:8787", timeout=30.0)

    def test_parser_exposes_explicit_opt_in_only_for_run(self):
        for flag in ([], ["--no-limits"]):
            with self.subTest(flag=flag):
                with patch("reflexmesh.runtime.cli.run_execution", return_value=0) as run:
                    code = main(["run", "--input", "task.json", "--output-dir", "unused",
                                 "--routing-provider", "stub", "--action-provider", "jev", *flag])
                self.assertEqual(code, 0)
                self.assertEqual(run.call_args.args[0].no_limits, bool(flag))
        with redirect_stderr(io.StringIO()), patch("reflexmesh.cli.read_task") as read:
            self.assertEqual(main(["route", "--no-limits"]), 2)
        read.assert_not_called()

    def test_cli_override_keeps_task_policy_and_emits_null_effective_limits(self):
        for no_limits in (False, True):
            with self.subTest(no_limits=no_limits), tempfile.TemporaryDirectory() as directory:
                data = task_data()
                original = copy.deepcopy(data)
                output = Path(directory) / "run"
                stdout = io.StringIO()
                with (patch("reflexmesh.runtime.cli._load", return_value=data),
                      patch("reflexmesh.runtime.cli.AttemptSupervisor") as supervisor,
                      patch("reflexmesh.runtime.cli.signal.signal"), redirect_stdout(stdout)):

                    def result():
                        task = supervisor.call_args.args[0]
                        return {"attempt_status": "completed", "verification": [{
                                    "id": "runtime.budget", "status": "pass",
                                    "scope": "steps_and_locally_controlled_model_calls",
                                    "evidence_refs": ["runtime:test:budget"]}], "actions": [],
                                "trace": [], "budget": {"limits": {
                                    "wall_seconds": task.limits.wall_seconds,
                                    "max_steps": task.limits.max_steps,
                                    "max_model_calls": task.limits.max_model_calls}}}

                    supervisor.return_value.run.side_effect = result
                    self.assertEqual(run_execution(self.args(output, no_limits=no_limits)), 0)
                effective = supervisor.call_args.args[0]
                expected = ExecutionTask.from_dict(original)
                self.assertEqual(effective.limits, Limits(None, None, None, 0) if no_limits else expected.limits)
                for field in ("goal", "permissions", "slots", "criteria", "allowed_executors"):
                    self.assertEqual(getattr(effective, field), getattr(expected, field))
                self.assertEqual(data, original)
                recorded = json.loads((output / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(json.loads(stdout.getvalue()), recorded)
                evidence = [json.loads(line) for line in (output / "evidence.jsonl").read_text().splitlines()]
                self.assertEqual(evidence[0]["scope"], "steps_and_locally_controlled_model_calls")
                self.assertEqual(recorded["budget"]["limits"], {
                    "wall_seconds": None if no_limits else 30,
                    "max_steps": None if no_limits else 2,
                    "max_model_calls": None if no_limits else 2,
                })
                json.dumps(recorded, allow_nan=False)

    def test_no_limits_does_not_bypass_input_validation(self):
        for invalid in ("budget", "permission", "retries"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                data = task_data()
                if invalid == "budget":
                    data["limits"]["max_steps"] = 0
                elif invalid == "permission":
                    data["permissions"] = ["run_shell"]
                else:
                    data["limits"]["max_action_retries"] = 1
                output = Path(directory) / "run"
                stderr = io.StringIO()
                with (patch("reflexmesh.runtime.cli._load", return_value=data),
                      patch("reflexmesh.runtime.cli.AttemptSupervisor") as supervisor,
                      redirect_stderr(stderr)):
                    self.assertEqual(run_execution(self.args(output, no_limits=True)), 2)
                supervisor.assert_not_called()
                self.assertFalse(output.exists())
                self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "invalid_input")


if __name__ == "__main__":
    unittest.main()
