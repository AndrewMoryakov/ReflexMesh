"""Immutable-slot runtime regressions with fake driver I/O, not a browser run.

Positive slot assessments use the production registry, handoff producer, ledger,
and assessor. Fake postconditions never stand in for immutable-slot evidence.
These tests intentionally require no installed SOH, Browser Use, or live model.
"""

import asyncio
import io
import json
import multiprocessing as mp
import os
import queue
import signal
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser
from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.cli import BrowserExecutionStrategy, run_execution
from reflexmesh.runtime.runner import (
    AttemptGate, AttemptSupervisor, ControlledEnvironment, RuntimeStop, WorkerResult,
)
from reflexmesh.text.slots import PreparedTextCommand, ResolvedSlot, SlotRegistry
from reflexmesh.tracing.slot_evidence import SlotEvidence
from reflexmesh.verification.constraints import REQUIRED_CONSTRAINTS, assess_constraints
from test_v05_preflight import baseline


PRIVATE_VALUE = "PRIVATE_SLOT_SENTINEL_7a9d\n  exact whitespace  "
WRONG_VALUE = "PRIVATE_MUTATED_SENTINEL_82ab"
SLOT_ID = "runtime.immutable_slots"
LINUX_WATCHDOG = (sys.platform.startswith("linux") and hasattr(os, "WNOWAIT") and
                  hasattr(signal, "setitimer") and "fork" in mp.get_all_start_methods())


def task_data(*, unlimited=False):
    return {
        "schema_version": "execution-task/0.1", "task_id": "immutable-slots-runtime",
        "revision": 1, "goal": "Fill the named fixture field once",
        "allowed_executors": ["browser.soh"],
        "fixture": {"origin": "http://127.0.0.1:8765", "run_id": "slots-runtime-1"},
        "start_path": "/form", "permissions": ["navigate", "type_text", "submit_form"],
        "text_slots": [{"id": "name", "version": 1, "value": PRIVATE_VALUE}],
        "criteria": [{"id": "intact", "kind": "postcondition",
                      "predicate": "account_intact", "args": {}}],
        "limits": None if unlimited else {
            "wall_seconds": 30, "max_steps": 8, "max_model_calls": 3,
            "max_action_retries": 0,
        },
    }


def make_task(*, unlimited=False):
    return ExecutionTask.from_dict(task_data(unlimited=unlimited))


def constraint(result, constraint_id=SLOT_ID):
    return next(row for row in result["verification"] if row["id"] == constraint_id)


def passing_postconditions(task, timeout):
    # This is deliberately only a postcondition fixture. No runtime constraint
    # is forged, patched, or passed through the postcondition verifier.
    return [{"id": criterion.id, "status": "pass", "evidence_refs": []}
            for criterion in task.criteria]


@contextmanager
def fake_result_module():
    package = ModuleType("systemone_harness")
    package.__path__ = []
    environment = ModuleType("systemone_harness.environment")
    environment.Result = SimpleNamespace
    package.environment = environment
    with patch.dict(sys.modules, {"systemone_harness": package,
                                  "systemone_harness.environment": environment}):
        yield


@contextmanager
def finite_watchdog(seconds=8.0):
    """A broken unlimited wait fails independently of the runtime's own gate."""
    if threading.current_thread() is not threading.main_thread():
        raise unittest.SkipTest("SIGALRM watchdog requires the main test thread")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def timed_out(*_):
        raise AssertionError("immutable-slot regression exceeded its watchdog")

    signal.signal(signal.SIGALRM, timed_out)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL,
                             max(0.001, previous_timer[0] - (time.monotonic() - started)),
                             previous_timer[1])


class FakeInputAction:
    def __init__(self, data):
        self.input = SimpleNamespace(**data)

    def model_dump(self, *, exclude_unset):
        return {"input": vars(self.input).copy()}


class FakeTextBackend:
    """Implements only the reviewed Tools.act input handoff and fixture reads."""

    def __init__(self, task, *, actual_calls=None, before_model=None,
                 mutate_model=False, timeout=False, on_driver=None):
        self.url = task.origin + task.start_path
        self.actual_calls = actual_calls
        self.before_model = before_model
        self.mutate_model, self.timeout = mutate_model, timeout
        self.on_driver = on_driver
        self.driver_inputs = []
        self._session = self
        self._tools = self
        self.nodes = {
            0: SimpleNamespace(attributes={"data-reflex-id": "name"},
                               backend_node_id=15, tag_name="input"),
            1: SimpleNamespace(attributes={"data-reflex-id": "email"},
                               backend_node_id=16, tag_name="input"),
        }
        # These are intentionally wrong: production text dispatch must never
        # consult the backend's mutable slot cache or its legacy execute path.
        self.text_values = {"name@1": WRONG_VALUE}

    async def get_selector_map(self):
        return self.nodes

    def _run(self, coroutine):
        return asyncio.run(coroutine)

    def observe(self):
        return SimpleNamespace(fields={"url": self.url}, candidates={
            "elements": {}, "text_fields": {"0": "Name", "1": "Email"},
        })

    def _action_model(self, *, input):
        if self.before_model is not None:
            self.before_model()
        model = FakeInputAction(input)
        if self.mutate_model:
            model.input.text = WRONG_VALUE
        return model

    async def act(self, model, session, sensitive_data=None):
        self.driver_inputs.append(model.input.text)
        if self.actual_calls is not None:
            self.actual_calls.value += 1
        if self.on_driver is not None:
            self.on_driver()
        if self.timeout:
            raise TimeoutError("driver response lost")
        return SimpleNamespace(error=None)

    def execute(self, action, params):
        raise AssertionError("legacy backend.execute must not handle text")

    def close(self):
        pass


def fixture_adapter(task, **backend_options):
    adapter = object.__new__(FixtureBrowser)
    adapter.task = task
    adapter.backend = FakeTextBackend(task, **backend_options)
    adapter.targets, adapter.observation_url, adapter.unsupported = {}, "", False
    # Slot-only fault injection; the synthetic driver cannot attest ownership.
    # Keep its guard bypass local and leave runtime.ownership unknown.
    adapter.bind_ownership = lambda owner: None
    adapter._assert_ownership = lambda **kwargs: None
    return adapter


def execute_text(task, gate, events, *, mismatched_command=False, **backend_options):
    adapter = fixture_adapter(task, **backend_options)
    environment = ControlledEnvironment(adapter, gate, events, adapter.admit)
    environment.observe()
    prepare = adapter.prepare

    def prepare_command(action, params):
        prepared = prepare(action, params)
        if mismatched_command:
            # Explicit corruption fault: substitute a private command carrying
            # the accepted reference and a different actual payload. The real
            # handoff forwards that payload to the fake driver, while the
            # independent supervisor registry proves the mismatch. A model
            # mutation rejected before handoff cannot establish such a failure.
            return replace(prepared, _text=PreparedTextCommand(
                0, ResolvedSlot("name@1", WRONG_VALUE)))
        return prepared

    with fake_result_module(), patch.object(adapter, "prepare", side_effect=prepare_command):
        environment.execute("type_text", {"field": "0", "value": "name@1"})
    return WorkerResult("finish")


class UninstrumentedAdapter:
    def __init__(self):
        self.calls = 0

    def observe(self):
        return SimpleNamespace(fields={})

    def describe(self, action, params):
        return {"operation": "type_text", "target_id": "name", "slot_ref": "name@1"}

    def execute(self, action, params):
        self.calls += 1
        return SimpleNamespace(ok=True)

    def prepare(self, action, params):
        raise AssertionError("a lookalike adapter cannot supply trusted classification")

    def bind_runtime(self, registry, sink):
        raise AssertionError("a lookalike adapter cannot acquire the trusted producer path")

    def execute_prepared(self, prepared, action_id):
        raise AssertionError("a lookalike adapter is not the exact reviewed adapter")


@unittest.skipUnless("fork" in mp.get_all_start_methods(), "shared gate requires fork")
class SlotGateGeneration(unittest.TestCase):
    def setUp(self):
        self.context = mp.get_context("fork")
        self.task = make_task(unlimited=True)
        self.registry = SlotRegistry.from_task(self.task)
        self.ledger = SlotEvidence(self.context, self.task, "gate-slot-attempt",
                                   registry=self.registry)
        self.addCleanup(self.ledger.close)
        self.gate = AttemptGate(self.context, self.task, time.monotonic(),
                                slot_registry=self.registry, slot_sink=self.ledger.sink)
        self.ledger.bind_dispatch_counter(self.gate.dispatches)

    def assessed_slot(self):
        self.ledger.drain()
        return next(row for row in assess_constraints(
            self.task, self.gate.snapshot(), "gate-slot-attempt",
            slot_evidence=self.ledger, coverage=self.gate.slot_snapshot())
            if row["id"] == SLOT_ID)

    def commit(self, classification="text", *, settle=True):
        self.gate.reserve_step()
        action_id = self.gate.commit_dispatch(classification)
        self.ledger.sink.commit(action_id, classification,
                                "name@1" if classification == "text" else None)
        if settle:
            self.gate.settle(action_id)
        return action_id

    def test_missing_handoff_cannot_pass_even_when_committed_and_settled(self):
        self.commit()
        coverage = self.gate.seal_dispatches()
        self.assertEqual(coverage, {"dispatches": 1, "text_dispatches": 1,
                                    "nontext_dispatches": 0, "unknown_dispatches": 0,
                                    "generation": 1, "sealed_generation": 1})
        row = self.assessed_slot()
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["evidence_refs"], [])

    def test_finish_requires_assessed_generation_to_be_sealed(self):
        self.commit("nontext")
        with self.assertRaises(RuntimeStop) as error:
            self.gate.finish(expected_generation=1)
        self.assertEqual(error.exception.reason, "evidence_changed")
        self.assertEqual(self.gate.terminal.value, 0)

    def test_stale_generation_cannot_accept_completion(self):
        previous = self.gate.slot_snapshot()["generation"]
        self.commit("nontext")
        current = self.gate.seal_dispatches()
        with self.assertRaises(RuntimeStop) as error:
            self.gate.finish(expected_generation=previous)
        self.assertEqual(error.exception.reason, "evidence_changed")
        self.assertEqual(self.gate.terminal.value, 0)
        self.assertEqual(self.gate.finish(expected_generation=current["generation"]), 3)

    def test_inflight_action_prevents_same_generation_completion(self):
        action_id = self.commit("nontext", settle=False)
        coverage = self.gate.seal_dispatches()
        with self.assertRaises(RuntimeStop) as error:
            self.gate.finish(expected_generation=coverage["generation"])
        self.assertEqual(error.exception.reason, "evidence_changed")
        self.gate.settle(action_id)
        self.assertEqual(self.gate.finish(expected_generation=coverage["generation"]), 3)

    def test_seal_blocks_new_dispatch_without_accepting_completion(self):
        self.commit("nontext")
        self.gate.seal_dispatches()
        self.assertEqual(self.gate.terminal.value, 0)
        with self.assertRaises(RuntimeStop) as error:
            self.gate.commit_dispatch("nontext")
        self.assertEqual(error.exception.reason, "dispatches_sealed")
        self.assertEqual(self.gate.dispatches.value, 1)
        self.assertTrue(self.gate.stop("cancel_requested"))

    def test_accepted_cancel_wins_even_over_stale_generation(self):
        self.commit("nontext")
        self.gate.seal_dispatches()
        self.assertTrue(self.gate.stop("cancel_requested"))
        self.assertEqual(self.gate.finish(expected_generation=0), 1)

    def test_cancel_between_prepare_and_commit_never_calls_driver(self):
        adapter = fixture_adapter(self.task)
        environment = ControlledEnvironment(adapter, self.gate, queue.Queue(), adapter.admit)
        environment.observe()
        prepare = adapter.prepare

        def prepare_then_cancel(action, params):
            prepared = prepare(action, params)
            self.assertTrue(self.gate.stop("cancel_requested"))
            return prepared

        with patch.object(adapter, "prepare", side_effect=prepare_then_cancel):
            with self.assertRaises(RuntimeStop) as error:
                environment.execute("type_text", {"field": "0", "value": "name@1"})
        self.assertEqual(error.exception.reason, "cancel_requested")
        self.assertEqual(self.gate.dispatches.value, 0)
        self.assertEqual(adapter.backend.driver_inputs, [])
        self.assertEqual(self.ledger.export(), [])

    def test_commit_before_cancel_allows_return_but_prevents_next_dispatch(self):
        adapter = fixture_adapter(self.task, on_driver=lambda: self.gate.stop("cancel_requested"))
        environment = ControlledEnvironment(adapter, self.gate, queue.Queue(), adapter.admit)
        environment.observe()
        with fake_result_module():
            returned = environment.execute("type_text", {"field": "0", "value": "name@1"})
        self.assertTrue(returned.ok)
        self.assertEqual(adapter.backend.driver_inputs, [PRIVATE_VALUE])
        self.assertEqual(self.gate.in_flight.value, 0)
        with self.assertRaises(RuntimeStop) as error:
            environment.execute("type_text", {"field": "1", "value": "name@1"})
        self.assertEqual(error.exception.reason, "cancel_requested")
        self.assertEqual(self.gate.dispatches.value, 1)
        self.gate.seal_dispatches()
        self.assertEqual(self.assessed_slot()["status"], "pass")

    def test_custom_admission_denial_still_blocks_trusted_adapter_before_commit(self):
        adapter = fixture_adapter(self.task)
        admitted = []

        def deny(action, params):
            admitted.append((action, params.copy()))
            return "policy_denied"

        environment = ControlledEnvironment(adapter, self.gate, queue.Queue(), deny)
        environment.observe()
        with self.assertRaises(RuntimeStop) as error:
            environment.execute("type_text", {"field": "0", "value": "name@1"})
        self.assertEqual(error.exception.reason, "policy_denied")
        self.assertEqual(admitted, [("type_text", {"field": "0", "value": "name@1"})])
        self.assertEqual(self.gate.dispatches.value, 0)
        self.assertEqual(adapter.backend.driver_inputs, [])

    def test_custom_policy_parameter_mutation_cannot_change_prepared_driver_command(self):
        adapter = fixture_adapter(self.task)
        admitted = []
        events = queue.Queue()

        def change_private_copy(action, params):
            admitted.append((action, params.copy()))
            params.update(field="1", value=WRONG_VALUE)
            return None

        environment = ControlledEnvironment(adapter, self.gate, events, change_private_copy)
        environment.observe()
        with fake_result_module():
            returned = environment.execute("type_text", {"field": "0", "value": "name@1"})
        self.assertTrue(returned.ok)
        self.assertEqual(admitted, [("type_text", {"field": "0", "value": "name@1"})])
        self.assertEqual(adapter.backend.driver_inputs, [PRIVATE_VALUE])
        dispatches = []
        while not events.empty():
            event = events.get_nowait()
            if event[0] == "dispatch":
                dispatches.append(event)
        self.assertEqual(dispatches, [("dispatch", 1, {
            "operation": "type_text", "target_id": "name", "slot_ref": "name@1",
        })])
        self.gate.seal_dispatches()
        self.assertEqual(self.assessed_slot()["status"], "pass")


@unittest.skipUnless(LINUX_WATCHDOG, "owned-worker tests require Linux and SIGALRM")
class ImmutableSlotLifecycle(unittest.TestCase):
    def run_bounded(self, supervisor, *, watchdog=8.0):
        try:
            with finite_watchdog(watchdog):
                result = supervisor.run()
            self.assertFalse(supervisor.process.is_alive(), "owned worker leaked")
            json.dumps(result, allow_nan=False)
            return result
        finally:
            if supervisor.process.pid is not None and not supervisor.process._released:
                supervisor.process.cleanup(1.0)
            supervisor.events.close()
            supervisor.slot_evidence.close()

    def test_real_handoff_and_budget_pass_leave_other_four_constraints_unknown(self):
        data = task_data()
        data["text_slots"].append({"id": "email", "version": 1, "value": "fixture@example.test"})
        data["criteria"] = [{"id": "sent", "kind": "postcondition", "predicate": "form_submitted_once",
                             "args": {"name_slot": "name@1", "email_slot": "email@1"}}]
        task = ExecutionTask.from_dict(data)
        calls = mp.get_context("fork").Value("i", 0)
        result = self.run_bounded(AttemptSupervisor(
            task, lambda gate, events: execute_text(task, gate, events, actual_calls=calls),
            passing_postconditions))
        self.assertEqual(calls.value, 1)
        self.assertEqual(result["verification"][0]["status"], "pass")
        self.assertEqual(result["actions"][0]["effect"], "applied")
        self.assertEqual(constraint(result)["status"], "pass")
        self.assertEqual(constraint(result, "runtime.budget")["status"], "pass")
        remaining = set(REQUIRED_CONSTRAINTS) - {SLOT_ID, "runtime.budget"}
        self.assertEqual(len(remaining), 4)
        self.assertTrue(all(constraint(result, cid)["status"] == "unknown" for cid in remaining))
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("incomplete", "verification_unknown", "unknown"))
        self.assertTrue(result["slot_evidence"])

    def test_timeout_after_correct_handoff_passes_slots_but_leaves_effect_unknown(self):
        task = make_task()
        calls = mp.get_context("fork").Value("i", 0)
        result = self.run_bounded(AttemptSupervisor(
            task, lambda gate, events: execute_text(task, gate, events,
                                                   actual_calls=calls, timeout=True)))
        self.assertEqual(calls.value, 1)
        self.assertEqual(constraint(result)["status"], "pass")
        self.assertNotEqual(result["attempt_status"], "completed")
        self.assertEqual(result["task_outcome"], "unknown")
        self.assertEqual(result["execution_outcome"], "unknown")
        self.assertEqual(result["actions"][0]["effect"], "unknown")

    def test_uninstrumented_and_prepared_lookalike_adapters_remain_unknown(self):
        class LegacyAdapter:
            def observe(self):
                return SimpleNamespace(fields={})

            def describe(self, action, params):
                return {"operation": "type_text", "target_id": "name", "slot_ref": "name@1"}

            def execute(self, action, params):
                return SimpleNamespace(ok=True)

        for adapter_type in (LegacyAdapter, UninstrumentedAdapter):
            with self.subTest(adapter=adapter_type.__name__):
                def strategy(gate, events):
                    environment = ControlledEnvironment(adapter_type(), gate, events, lambda *_: None)
                    environment.observe()
                    environment.execute("type_text", {"field": "0", "value": "name@1"})
                    return WorkerResult("finish")

                result = self.run_bounded(AttemptSupervisor(make_task(), strategy, passing_postconditions))
                self.assertEqual(result["budget"]["dispatches"], 1)
                self.assertEqual(constraint(result)["status"], "unknown")
                self.assertEqual(constraint(result)["evidence_refs"], [])
                self.assertNotEqual(result["attempt_status"], "completed")

    def test_unsupported_backend_after_commit_has_no_invented_receipt(self):
        task = make_task()

        def strategy(gate, events):
            adapter = fixture_adapter(task)
            adapter.backend._tools = None
            environment = ControlledEnvironment(adapter, gate, events, adapter.admit)
            environment.observe()
            environment.execute("type_text", {"field": "0", "value": "name@1"})
            return WorkerResult("finish")

        result = self.run_bounded(AttemptSupervisor(task, strategy))
        self.assertEqual(result["budget"]["dispatches"], 1)
        self.assertEqual(constraint(result)["status"], "unknown")
        self.assertEqual(constraint(result)["evidence_refs"], [])
        self.assertNotEqual(result["attempt_status"], "completed")

    def test_model_mutation_rejected_before_handoff_is_unknown_not_confirmed_failure(self):
        task = make_task()
        calls = mp.get_context("fork").Value("i", 0)
        result = self.run_bounded(AttemptSupervisor(task, lambda gate, events: execute_text(
            task, gate, events, actual_calls=calls, mutate_model=True)))
        self.assertEqual(calls.value, 0)
        self.assertEqual(result["budget"]["dispatches"], 1)
        self.assertEqual(constraint(result)["status"], "unknown")
        self.assertEqual(constraint(result)["evidence_refs"], [])
        self.assertEqual(result["task_outcome"], "unknown")

    def test_worker_crash_after_commit_without_receipt_remains_unknown(self):
        def strategy(gate, events):
            gate.reserve_step()
            gate.commit_dispatch("text")
            os._exit(23)

        result = self.run_bounded(AttemptSupervisor(make_task(), strategy))
        self.assertEqual((result["attempt_status"], result["stop_reason"]),
                         ("failed", "executor_error"))
        self.assertEqual(result["budget"]["dispatches"], 1)
        self.assertEqual(result["actions"], [{"id": 1, "effect": "unknown"}])
        self.assertEqual(constraint(result)["status"], "unknown")
        self.assertEqual(constraint(result)["evidence_refs"], [])

    def test_budget_snapshot_failure_cannot_erase_proven_slot_mismatch(self):
        task = make_task()
        calls = mp.get_context("fork").Value("i", 0)
        supervisor = AttemptSupervisor(task, lambda gate, events: execute_text(
            task, gate, events, actual_calls=calls, mismatched_command=True))
        with patch.object(supervisor.gate, "snapshot", side_effect=RuntimeStop("gate_unavailable")):
            result = self.run_bounded(supervisor)
        self.assertEqual(calls.value, 1)
        self.assertEqual(constraint(result)["status"], "fail")
        self.assertTrue(constraint(result)["evidence_refs"])
        self.assertEqual(constraint(result, "runtime.budget")["status"], "unknown")
        self.assertEqual(result["task_outcome"], "fail")
        self.assertEqual(result["cleanup"], "unknown")

    def test_actual_mismatch_stays_failed_after_later_matching_handoff(self):
        task = make_task()
        calls = mp.get_context("fork").Value("i", 0)

        def strategy(gate, events):
            adapter = fixture_adapter(task, actual_calls=calls)
            environment = ControlledEnvironment(adapter, gate, events, adapter.admit)
            environment.observe()
            prepare = adapter.prepare

            def corrupt_first_command(action, params):
                return replace(prepare(action, params), _text=PreparedTextCommand(
                    0, ResolvedSlot("name@1", WRONG_VALUE)))

            with fake_result_module():
                with patch.object(adapter, "prepare", side_effect=corrupt_first_command):
                    environment.execute("type_text", {"field": "0", "value": "name@1"})
                environment.observe()
                environment.execute("type_text", {"field": "1", "value": "name@1"})
            return WorkerResult("finish")

        result = self.run_bounded(AttemptSupervisor(task, strategy))
        self.assertEqual(calls.value, 2)
        self.assertEqual(result["budget"]["dispatches"], 2)
        self.assertEqual(constraint(result)["status"], "fail")
        self.assertEqual(result["task_outcome"], "fail")
        handoffs = {row["action_id"]: row["status"] for row in result["slot_evidence"]
                    if row["kind"] == "handoff"}
        self.assertEqual(handoffs, {1: "fail", 2: "pass"})

    def late_handoff_after_cancel(self, *, mismatch):
        task = make_task(unlimited=True)
        context = mp.get_context("fork")
        committed, release = context.Event(), context.Event()
        calls = context.Value("i", 0)

        def pause_model():
            # This model is built after gate.commit_dispatch(), and before the
            # production actual-payload witness. No forged receipt is injected.
            committed.set()
            if not release.wait(3.0):
                raise RuntimeError("test did not release the prepared model")

        supervisor = AttemptSupervisor(task, lambda gate, events: execute_text(
            task, gate, events, actual_calls=calls,
            before_model=pause_model, mismatched_command=mismatch), passing_postconditions)
        cancellation = []

        def cancel_then_release():
            ready = committed.wait(3.0)
            cancellation.append((ready, supervisor.cancel()))
            release.set()

        thread = threading.Thread(target=cancel_then_release, daemon=True)
        thread.start()
        try:
            result = self.run_bounded(supervisor)
        finally:
            release.set()
            thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(cancellation, [(True, True)])
        self.assertEqual((result["attempt_status"], result["stop_reason"]),
                         ("cancelled", "cancel_requested"))
        self.assertEqual(result["budget"]["dispatches"], 1)
        self.assertEqual(calls.value, 1)
        self.assertEqual(constraint(result)["status"], "fail" if mismatch else "pass")
        self.assertEqual(result["task_outcome"], "fail" if mismatch else "unknown")
        self.assertEqual(result["actions"][0]["effect"], "unknown")
        return result

    def test_late_matching_handoff_cannot_complete_accepted_cancellation(self):
        self.late_handoff_after_cancel(mismatch=False)

    def test_late_mismatch_keeps_cancellation_and_refines_outcome_to_fail(self):
        self.late_handoff_after_cancel(mismatch=True)

    def test_dead_worker_holding_gate_has_bounded_recovery_and_unknown_evidence(self):
        def strategy(gate, events):
            gate.reserve_step()
            gate.commit_dispatch("text")
            gate.lock.acquire()
            os._exit(24)

        started = time.monotonic()
        result = self.run_bounded(AttemptSupervisor(make_task(unlimited=True), strategy))
        self.assertLess(time.monotonic() - started, 6.0)
        self.assertEqual(result["attempt_status"], "failed")
        self.assertEqual(result["cleanup"], "unknown")
        self.assertEqual(result["execution_outcome"], "unknown")
        self.assertEqual(constraint(result)["status"], "unknown")
        self.assertEqual(constraint(result, "runtime.budget")["status"], "unknown")

    def test_unlimited_multi_hour_task_keeps_real_slot_evidence_and_null_limits(self):
        task = make_task(unlimited=True)
        supervisor = AttemptSupervisor(task, lambda gate, events: execute_text(task, gate, events))
        supervisor.started -= 3 * 60 * 60
        supervisor.gate.started = supervisor.started
        result = self.run_bounded(supervisor)
        self.assertGreaterEqual(result["budget"]["elapsed_seconds"], 3 * 60 * 60)
        self.assertEqual(result["budget"]["limits"], {
            "wall_seconds": None, "max_steps": None, "max_model_calls": None,
        })
        for field in ("remaining_wall_seconds", "remaining_steps", "remaining_model_calls"):
            self.assertIsNone(result["budget"][field])
        self.assertEqual(constraint(result)["status"], "pass")
        self.assertEqual(result["attempt_status"], "incomplete")

    def test_opaque_jev_router_preserves_unknown_usage_with_unlimited_task(self):
        task = make_task(unlimited=True)
        args = SimpleNamespace(routing_provider="jevrouter", jev_url="http://127.0.0.1:8787",
                               timeout=30.0, chrome=None)

        def two_stage_route(request, **kwargs):
            return {"schema_version": "decision/0.1", "task_id": request.task_id,
                    "status": "abstained", "route": None, "reason": "test_no_route"}

        with (patch("reflexmesh.runtime.cli.claim_fixture", baseline),
              patch("reflexmesh.routing.router.jev_route", side_effect=two_stage_route)):
            result = self.run_bounded(AttemptSupervisor(task, BrowserExecutionStrategy(task, args, None)))
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("blocked", "no_route"))
        self.assertIsNone(result["budget"]["model_calls"])
        self.assertEqual(result["budget"]["local_model_calls"], 0)
        self.assertEqual(result["budget"]["model_calls_reason"], "router_internal_usage_unknown")
        self.assertEqual(result["router_usage"], {"model_calls": None})
        self.assertEqual(result["router_capabilities"], {
            "router_id": "jevrouter", "model_call_accounting": "opaque",
        })
        self.assertEqual(constraint(result, "runtime.budget")["scope"],
                         "steps_and_locally_controlled_model_calls")


@unittest.skipUnless(LINUX_WATCHDOG, "CLI integration uses the Linux owned worker")
class ImmutableSlotArtifacts(unittest.TestCase):
    def args(self, output):
        return SimpleNamespace(input="task.json", output_dir=str(output), no_limits=True,
                               routing_provider="stub", action_provider="jev", script=None,
                               chrome=None, jev_url="http://127.0.0.1:8787", timeout=30.0)

    def test_cli_persists_real_redacted_records_and_every_slot_ref_resolves(self):
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "result"
                stdout = io.StringIO()

                def strategy_factory(task, args, entries):
                    return lambda gate, events: execute_text(task, gate, events, mismatched_command=mismatch)

                with (finite_watchdog(),
                      patch("reflexmesh.runtime.cli._load", return_value=task_data()),
                      patch("reflexmesh.runtime.cli.BrowserExecutionStrategy", side_effect=strategy_factory),
                      patch("reflexmesh.runtime.cli.signal.signal"),
                      redirect_stdout(stdout)):
                    code = run_execution(self.args(output))
                self.assertNotEqual(code, 0)
                result_text = (output / "result.json").read_text(encoding="utf-8")
                evidence_text = (output / "evidence.jsonl").read_text(encoding="utf-8")
                trace_text = (output / "trace.jsonl").read_text(encoding="utf-8")
                result = json.loads(result_text)
                self.assertEqual(json.loads(stdout.getvalue()), result)
                self.assertEqual(result["evidence_persistence"], "stored")
                self.assertEqual(constraint(result)["status"], "fail" if mismatch else "pass")
                records = [json.loads(line) for line in evidence_text.splitlines()]
                by_ref = {record["ref"]: record for record in records}
                self.assertTrue(result["slot_evidence"])
                for record in result["slot_evidence"]:
                    self.assertIn(record, records)
                for ref in constraint(result)["evidence_refs"]:
                    self.assertIn(ref, by_ref)
                    self.assertIn(by_ref[ref], result["slot_evidence"])
                    self.assertNotIn("fake://", ref)
                serialized = "\n".join((result_text, evidence_text, trace_text, stdout.getvalue()))
                self.assertNotIn("PRIVATE_SLOT_SENTINEL_7a9d", serialized)
                self.assertNotIn(WRONG_VALUE, serialized)
                self.assertNotIn("test-only:", serialized)
                self.assertNotIn("fake://", serialized)
                self.assertIn("name@1", serialized)
                json.dumps(result, allow_nan=False)

    def test_output_failure_clears_unstored_refs_without_erasing_confirmed_mismatch(self):
        stdout = io.StringIO()

        def strategy_factory(task, args, entries):
            return lambda gate, events: execute_text(task, gate, events, mismatched_command=True)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            with (finite_watchdog(),
                  patch("reflexmesh.runtime.cli._load", return_value=task_data()),
                  patch("reflexmesh.runtime.cli.BrowserExecutionStrategy", side_effect=strategy_factory),
                  patch("reflexmesh.runtime.cli.signal.signal"),
                  patch.object(Path, "write_text", side_effect=OSError("disk unavailable")),
                  redirect_stdout(stdout)):
                self.assertEqual(run_execution(self.args(output)), 5)
        result = json.loads(stdout.getvalue())
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("failed", "output_error", "fail"))
        self.assertEqual(result["evidence_persistence"], "unavailable")
        self.assertIsNone(result["trace_ref"])
        self.assertEqual(result["evidence_refs"], [])
        self.assertEqual(result["slot_evidence"], [])
        self.assertEqual(constraint(result)["status"], "fail")
        self.assertTrue(all(row.get("evidence_refs", []) == [] for row in result["verification"]))
        self.assertTrue(all(row.get("evidence_refs", []) == [] for row in result["actions"]))
        self.assertNotIn("PRIVATE_SLOT_SENTINEL_7a9d", stdout.getvalue())
        self.assertNotIn(WRONG_VALUE, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
