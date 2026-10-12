"""Fault wiring regressions. Source prepared under a no-run instruction.

These cases use runtime fakes, not real browser/Jev acceptance evidence. The
blocking worker cases have independent watchdogs and do not loosen completion.
"""

import asyncio
import copy
import io
import json
import multiprocessing as mp
import queue
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.adapters.system_one.harness import CountingProvider
from reflexmesh.api.service import ApiError, TaskService
from reflexmesh.contracts.execution import ExecutionTask, Limits
from reflexmesh.contracts.task import ValidationError
from reflexmesh.runtime.cli import BrowserExecutionStrategy, run_execution
from reflexmesh.runtime.faults import FaultBarriers, LostDriverReturn, validate_faults
from reflexmesh.runtime.runner import AttemptGate, AttemptSupervisor, ControlledEnvironment, RuntimeStop, WorkerResult
from test_v05_optional_limits import fake_harness_modules, finite_watchdog
from test_v05_runtime import sample


def make_task():
    raw = sample()
    raw["limits"].update(wall_seconds=30, max_steps=8, max_model_calls=8)
    return ExecutionTask.from_dict(raw)


def make_gate(task=None):
    return AttemptGate(mp.get_context("fork"), task or make_task(), time.monotonic())


def cli_args(directory):
    return SimpleNamespace(input="task.json", faults="faults.json", output_dir=str(directory / "out"),
                           control_dir=str(directory), no_limits=False, routing_provider="stub",
                           action_provider="jev", script=None, chrome=sys.executable,
                           jev_url="http://127.0.0.1:8787", timeout=30)


class FakeEnvironment:
    def __init__(self):
        self.executions = 0
        self.closed = False

    def observe(self):
        return SimpleNamespace(fields={}, candidates={})

    def admit(self, action, params):
        return None

    def describe(self, action, params):
        return {"operation": "submit_form", "target_id": "send-form"}

    def execute(self, action, params):
        self.executions += 1
        return SimpleNamespace(ok=True)

    def close(self):
        self.closed = True


class FaultValidation(unittest.TestCase):
    def test_validated_configuration_does_not_alias_nested_input(self):
        raw = {"hold_decision": {"at": 2, "seconds": 600}}
        captured = validate_faults(raw)
        raw["hold_decision"]["seconds"] = 0
        self.assertEqual(captured["hold_decision"]["seconds"], 600)

    def test_invalid_barriers_are_rejected(self):
        for raw in ({"unknown": True}, {"hold_decision": {"at": True, "seconds": 1}},
                    {"hold_decision": {"at": 1, "seconds": float("inf")}},
                    {"hold_driver": {"target_id": "", "until": 1}},
                    {"corrupt_budget_after_cancel": True}):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                validate_faults(raw)

    def test_deadline_and_counter_faults_require_enabled_limits(self):
        unlimited = Limits(None, None, None, 0)
        for raw in ({"hold_driver": {"target_id": "send-form", "until": "deadline"}},
                    {"cancel_after_commit": "send-form", "corrupt_budget_after_cancel": True}):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                validate_faults(raw, limits=unlimited)
        validate_faults({"hold_decision": {"at": 1, "seconds": 600}}, limits=unlimited)

    def test_service_rejects_impossible_fault_before_launch(self):
        for faults in ({"hold_driver": {"target_id": "send-form", "until": "deadline"}},
                       {"cancel_after_commit": "send-form", "corrupt_budget_after_cancel": True}):
            with self.subTest(faults=faults), tempfile.TemporaryDirectory() as directory:
                service = TaskService(Path(directory) / "state", allow_faults=True)
                raw = sample()
                raw["limits"] = None
                try:
                    with patch.object(service, "_start") as launch, self.assertRaises(ApiError) as caught:
                        service.submit(raw, "stub", "script", [{"action": "finish"}], faults)
                    self.assertEqual(caught.exception.code, "invalid_input")
                    launch.assert_not_called()
                    self.assertEqual(list((service.root / "attempts").iterdir()), [])
                finally:
                    service.shutdown()

    def test_service_faults_remain_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            service = TaskService(Path(directory) / "state")
            try:
                with patch.object(service, "_start") as launch, self.assertRaises(ApiError):
                    service.submit(sample(), "stub", "script", [{"action": "finish"}],
                                   {"hold_decision": {"at": 1, "seconds": 1}})
                launch.assert_not_called()
            finally:
                service.shutdown()

    def test_invalid_continuation_fault_does_not_consume_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = TaskService(Path(temporary) / "state", allow_faults=True)
            parent_id = "a" * 32
            parent = service.root / "attempts" / parent_id
            (parent / "out").mkdir(parents=True)
            raw = sample()
            raw["limits"] = None
            (parent / "task.json").write_text(json.dumps(raw))
            (parent / "request.json").write_text(json.dumps({"attempt_id": parent_id}))
            (parent / "out" / "result.json").write_text(json.dumps({
                "attempt_status": "incomplete", "handoff": {"continuation": {"allowed": True}},
                "chain": {"root_attempt_id": parent_id, "parent_attempt_id": None, "sequence": 1,
                          "used": {"steps": 1, "model_calls": 1, "wall_seconds": 1}},
            }))
            try:
                with patch.object(service, "_start") as launch, self.assertRaises(ApiError) as caught:
                    service.continue_task(parent_id, {}, "stub", "script", [{"action": "finish"}],
                                          {"hold_driver": {"target_id": "send-form", "until": "deadline"}})
                self.assertEqual(caught.exception.code, "invalid_input")
                launch.assert_not_called()
                self.assertFalse((parent / "continuation.json").exists())
            finally:
                service.shutdown()


class DecisionBarriers(unittest.TestCase):
    def test_hold_announces_exact_request_outside_gate_and_reserves_once(self):
        with tempfile.TemporaryDirectory() as directory:
            gate, events = make_gate(), queue.Queue()
            barriers = FaultBarriers({"hold_decision": {"at": 2, "seconds": 3}}, gate, events,
                                     control_dir=directory)
            inner = Mock()
            provider = CountingProvider(inner, gate, events, model=True, faults=barriers)
            provider.decide({}, {})
            marker = Path(directory) / "fault-hold-started"
            self.assertFalse(marker.exists())

            def held(seconds):
                self.assertEqual(seconds, 3)
                self.assertTrue(marker.is_file())
                self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
                self.assertTrue(gate.lock.acquire(timeout=0.1))
                gate.lock.release()
                self.assertEqual(gate.calls.value, 2)
                self.assertEqual(inner.decide.call_count, 1)

            with patch("reflexmesh.runtime.faults.time.sleep", side_effect=held):
                provider.decide({}, {})
            self.assertEqual(inner.decide.call_count, 2)
            self.assertEqual(gate.calls.value, 2)

    def test_cancellation_during_hold_prevents_late_provider_and_dispatch(self):
        gate, events = make_gate(), queue.Queue()
        inner = Mock()
        provider = CountingProvider(inner, gate, events, model=True, faults=FaultBarriers(
            {"hold_decision": {"at": 1, "seconds": 1}}, gate, events))
        with (patch("reflexmesh.runtime.faults.time.sleep", side_effect=lambda _: gate.stop("cancel_requested")),
              self.assertRaises(RuntimeStop) as caught):
            provider.decide({}, {})
        self.assertEqual(caught.exception.reason, "cancel_requested")
        inner.decide.assert_not_called()
        self.assertEqual((gate.calls.value, gate.dispatches.value), (1, 0))

    def test_late_actual_provider_answer_is_not_returned_after_cancel(self):
        gate, events = make_gate(), queue.Queue()
        inner = Mock()
        inner.decide.side_effect = lambda *_: gate.stop("cancel_requested")
        with self.assertRaises(RuntimeStop):
            CountingProvider(inner, gate, events, model=True).decide({}, {})
        self.assertEqual(gate.calls.value, 1)

    def test_call_budget_rejects_before_announcing_hold(self):
        gate, events = make_gate(), queue.Queue()
        gate.calls.value = gate.max_calls
        barriers = Mock()
        with self.assertRaises(RuntimeStop) as caught:
            CountingProvider(Mock(), gate, events, model=True, faults=barriers).decide({}, {})
        self.assertEqual(caught.exception.reason, "model_call_limit")
        barriers.before_decision.assert_not_called()
        self.assertTrue(events.empty())

    def test_injected_cancel_uses_supervisor_signal_path(self):
        gate, events = make_gate(), queue.Queue()
        inner = Mock()
        barriers = FaultBarriers({"cancel_on_decision": 1}, gate, events)
        with (patch("reflexmesh.runtime.faults.request_cancel",
                    side_effect=lambda current: current.stop("cancel_requested")) as cancel,
              self.assertRaises(RuntimeStop)):
            CountingProvider(inner, gate, events, model=False, faults=barriers).decide({}, {})
        cancel.assert_called_once_with(gate)
        inner.decide.assert_not_called()

    def test_provider_error_and_hang_trigger_without_a_provider_call(self):
        for name in ("provider_error_at", "hang_provider_at"):
            with self.subTest(name=name):
                gate, events = make_gate(), queue.Queue()
                inner = Mock()
                barriers = FaultBarriers({name: 1}, gate, events)
                with (patch("reflexmesh.runtime.faults.sleep_forever", side_effect=ValueError("held")),
                      self.assertRaises(ValueError)):
                    CountingProvider(inner, gate, events, model=True, faults=barriers).decide({}, {})
                inner.decide.assert_not_called()
                self.assertEqual(gate.calls.value, 1)


class DispatchBarriers(unittest.TestCase):
    def controlled(self, faults):
        gate, events, inner = make_gate(), queue.Queue(), FakeEnvironment()
        barriers = FaultBarriers(faults, gate, events)
        controlled = ControlledEnvironment(inner, gate, events, inner.admit, faults=barriers)
        controlled.observe()
        return controlled, inner, gate, events

    def test_cancel_after_revalidation_prevents_commit_and_driver(self):
        env, inner, gate, _ = self.controlled({"cancel_before_commit": "send-form"})
        with (patch("reflexmesh.runtime.faults.request_cancel",
                    side_effect=lambda current: current.stop("cancel_requested")),
              self.assertRaises(RuntimeStop)):
            env.execute("click", {"element": "1"})
        self.assertEqual((gate.dispatches.value, inner.executions), (0, 0))

    def test_revocation_at_commit_retains_atomic_policy_check_and_evidence(self):
        env, inner, gate, events = self.controlled({"revoke_on_commit": "submit_form"})
        with self.assertRaises(RuntimeStop) as caught:
            env.execute("click", {"element": "1"})
        self.assertEqual(caught.exception.reason, "policy_denied")
        self.assertEqual((gate.dispatches.value, inner.executions), (0, 0))
        rows = list(events.queue)
        self.assertIn(("revocation", "submit_form", 1), rows)
        self.assertFalse(gate.permitted("submit_form"))

    def test_dispatch_winner_is_in_flight_and_never_commits_again(self):
        env, inner, gate, events = self.controlled({"cancel_after_commit": "send-form"})
        with patch("reflexmesh.runtime.faults.request_cancel",
                   side_effect=lambda current: current.stop("cancel_requested")):
            env.execute("click", {"element": "1"})
        self.assertEqual((inner.executions, gate.dispatches.value), (1, 1))
        dispatch = next(row for row in list(events.queue) if row[0] == "dispatch")
        self.assertLess(dispatch[2]["gate_seq"], gate.terminal_seq.value)
        with self.assertRaises(RuntimeStop):
            env.observe()
        self.assertEqual(gate.dispatches.value, 1)

    def test_corruption_only_adds_negative_evidence_after_accepted_cancel(self):
        env, _, gate, _ = self.controlled({"cancel_after_commit": "send-form",
                                           "corrupt_budget_after_cancel": True})
        limits = (gate.max_steps, gate.max_calls)
        with patch("reflexmesh.runtime.faults.request_cancel",
                   side_effect=lambda current: current.stop("cancel_requested")):
            env.execute("click", {"element": "1"})
        self.assertEqual(gate.terminal.value, 1)
        self.assertGreater(gate.steps.value, gate.max_steps)
        self.assertEqual((gate.max_steps, gate.max_calls), limits)

    def test_driver_hold_occurs_after_driver_and_outside_gate(self):
        env, inner, gate, _ = self.controlled({"hold_driver": {"target_id": "send-form", "until": 2}})

        def held(seconds):
            self.assertEqual(seconds, 2)
            self.assertEqual(inner.executions, 1)
            self.assertEqual(gate.in_flight.value, 1)
            self.assertTrue(gate.lock.acquire(timeout=0.1))
            gate.lock.release()

        with patch("reflexmesh.runtime.faults.time.sleep", side_effect=held):
            env.execute("click", {"element": "1"})
        self.assertEqual(gate.in_flight.value, 0)

    def test_deadline_hold_uses_original_absolute_deadline(self):
        env, _, gate, _ = self.controlled({"hold_driver": {"target_id": "send-form", "until": "deadline"}})
        deadline = gate.deadline
        with patch("reflexmesh.runtime.faults.time.sleep") as sleep:
            env.execute("click", {"element": "1"})
        self.assertGreater(sleep.call_args.args[0], 0)
        self.assertLessEqual(sleep.call_args.args[0], 30.1)
        self.assertEqual(gate.deadline, deadline)

    def test_lost_return_retains_effect_uncertainty_and_prevents_repeat(self):
        env, inner, gate, _ = self.controlled({"lose_return_on": "send-form"})
        result = env.execute("click", {"element": "1"})
        self.assertIsInstance(result, LostDriverReturn)
        self.assertFalse(result.to_dict()["ok"])
        with self.assertRaises(RuntimeStop) as caught:
            env.execute("click", {"element": "1"})
        self.assertEqual(caught.exception.reason, "effect_unknown")
        self.assertEqual((inner.executions, gate.dispatches.value), (1, 1))

    def test_driver_and_close_hangs_are_at_their_declared_boundaries(self):
        env, inner, _, _ = self.controlled({"hang_driver_on": "send-form"})
        with patch("reflexmesh.runtime.faults.sleep_forever", side_effect=RuntimeError("held")):
            with self.assertRaises(RuntimeError):
                env.execute("click", {"element": "1"})
        self.assertEqual(inner.executions, 1)
        self.assertTrue(env.uncertain_mutation)
        env, inner, _, _ = self.controlled({"hang_close": True})
        with patch("reflexmesh.runtime.faults.sleep_forever", side_effect=RuntimeError("held")):
            with self.assertRaises(RuntimeError):
                env.close()
        self.assertFalse(inner.closed)


class FixtureReplacementBarrier(unittest.TestCase):
    """The CDP doubles verify the fixed probe contract, not a live DOM change."""

    def fixture(self, *, resolved=None, returned=None):
        from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser

        inner = FixtureBrowser(make_task())
        inner.targets = {"4": (71, "send-form", "button", "submit", "")}
        inner.observation_url = inner.task.origin + "/form"
        inner._assert_ownership = Mock(return_value={})
        inner._check_private_transport = Mock()
        calls = []

        async def resolve(*, params, session_id):
            calls.append(("resolve", params, session_id))
            return resolved if resolved is not None else {"object": {"objectId": "observed-object"}}

        async def replace_node(*, params, session_id):
            calls.append(("replace", params, session_id))
            return returned if returned is not None else {"result": {"value": True}}

        cdp = SimpleNamespace(session_id="owned-cdp", cdp_client=SimpleNamespace(send=SimpleNamespace(
            DOM=SimpleNamespace(resolveNode=resolve), Runtime=SimpleNamespace(callFunctionOn=replace_node))))

        async def get_cdp():
            return cdp

        inner._owned_session = SimpleNamespace(get_or_create_cdp_session=get_cdp)
        inner.backend = SimpleNamespace(_run=asyncio.run)
        return inner, calls

    def test_replacement_uses_observed_node_and_fixed_guarded_function(self):
        inner, calls = self.fixture()
        targets = copy.deepcopy(inner.targets)
        inner.replace_target_for_acceptance("send-form")
        self.assertEqual(calls[0], ("resolve", {"backendNodeId": 71}, "owned-cdp"))
        params = calls[1][1]
        self.assertEqual(params["objectId"], "observed-object")
        self.assertEqual(params["arguments"], [{"value": inner.observation_url}, {"value": "send-form"}])
        for guard in ("this.isConnected", "window !== window.top", "this.ownerDocument !== document",
                      "document.location.href !== url", "getAttribute('data-reflex-id') !== target"):
            self.assertIn(guard, params["functionDeclaration"])
        self.assertNotIn("send-form", params["functionDeclaration"])
        self.assertNotIn("secret-text", json.dumps(calls))
        self.assertEqual(inner.targets, targets)

    def test_missing_observed_target_and_foreign_url_fail_before_cdp(self):
        for invalid in ("missing", "foreign_url"):
            with self.subTest(invalid=invalid):
                inner, calls = self.fixture()
                if invalid == "foreign_url":
                    inner.observation_url = "https://other.example/"
                with self.assertRaises(RuntimeStop):
                    inner.replace_target_for_acceptance("missing" if invalid == "missing" else "send-form")
                self.assertEqual(calls, [])

    def test_missing_remote_object_does_not_call_replacement(self):
        inner, calls = self.fixture(resolved={"object": {"type": "undefined"}})
        with self.assertRaises(RuntimeStop) as caught:
            inner.replace_target_for_acceptance("send-form")
        self.assertEqual(caught.exception.reason, "adapter_contract_unsupported")
        self.assertEqual([row[0] for row in calls], ["resolve"])

    def test_failed_guard_or_protocol_result_never_reports_applied_probe(self):
        for returned in ({"result": {"value": False}}, {"result": {"value": "true"}},
                         {"exceptionDetails": {"text": "stale"}, "result": {"value": True}}):
            with self.subTest(returned=returned):
                inner, _ = self.fixture(returned=returned)
                with self.assertRaises(RuntimeStop) as caught:
                    inner.replace_target_for_acceptance("send-form")
                self.assertEqual(caught.exception.reason, "adapter_contract_unsupported")

    def test_ownership_failure_after_resolution_prevents_replacement(self):
        inner, calls = self.fixture()
        inner._assert_ownership.side_effect = [{}, {}, {}, RuntimeStop("ownership_lost")]
        with self.assertRaises(RuntimeStop) as caught:
            inner.replace_target_for_acceptance("send-form")
        self.assertEqual(caught.exception.reason, "ownership_lost")
        self.assertEqual([row[0] for row in calls], ["resolve"])

    def test_probe_is_one_shot_and_followed_by_normal_stale_admission(self):
        inner, _ = self.fixture()
        order = []
        inner.replace_target_for_acceptance = Mock(side_effect=lambda _: order.append("replace"))

        def stale(*_):
            order.append("prepare")
            raise RuntimeStop("stale_target")

        inner.prepare = Mock(side_effect=stale)
        inner.bind_profile_owner = Mock()
        inner.bind_ownership = Mock()
        inner.bind_runtime = Mock()
        gate, events = make_gate(), queue.Queue()
        barriers = FaultBarriers({"replace_before_admit": "send-form"}, gate, events)
        controlled = ControlledEnvironment(inner, gate, events, inner.admit, faults=barriers)
        self.assertTrue(controlled._prepared_protocol)
        self.assertFalse(controlled.execute("click", {"element": "4"}).ok)
        self.assertFalse(controlled.execute("click", {"element": "4"}).ok)
        self.assertEqual(order, ["replace", "prepare", "prepare"])
        self.assertEqual(gate.dispatches.value, 0)
        self.assertIn(("reobserve", "stale_target"), list(events.queue))

    def test_denied_target_is_not_replaced(self):
        inner, _ = self.fixture()
        inner.replace_target_for_acceptance = Mock()
        gate = make_gate()
        gate.revoke("submit_form")
        FaultBarriers({"replace_before_admit": "send-form"}, gate, queue.Queue()).before_admit(
            inner, "click", {"element": "4"})
        inner.replace_target_for_acceptance.assert_not_called()


class CliFaultWiring(unittest.TestCase):
    def test_cli_passes_faults_to_strategy_and_records_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            faults = {"hold_decision": {"at": 2, "seconds": 600}}
            result = {"attempt_status": "blocked", "stop_reason": "executor_unavailable",
                      "task_outcome": "unknown", "verification": [], "actions": [], "trace": [], "budget": {}}
            with (patch("reflexmesh.runtime.cli._load", side_effect=[sample(), faults]),
                  patch("reflexmesh.runtime.cli.AttemptSupervisor") as supervisor,
                  patch("reflexmesh.runtime.cli.signal.signal"), redirect_stdout(io.StringIO())):
                supervisor.return_value.run.return_value = result
                self.assertEqual(run_execution(cli_args(directory)), 3)
            strategy = supervisor.call_args.args[1]
            self.assertEqual(strategy.faults, faults)
            recorded = json.loads((directory / "out" / "result.json").read_text())
            self.assertEqual(recorded["faults"], faults)
            self.assertEqual(recorded["trace"], [])  # Configured is not claimed as reached.

    def test_cli_rejects_malformed_faults_before_attempt_and_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with (patch("reflexmesh.runtime.cli._load", side_effect=[sample(), {"hold_decision": {}}]),
                  patch("reflexmesh.runtime.cli.AttemptSupervisor") as supervisor,
                  redirect_stderr(io.StringIO())):
                self.assertEqual(run_execution(cli_args(directory)), 2)
            supervisor.assert_not_called()
            self.assertFalse((directory / "out").exists())

    def test_browser_strategy_preserves_exact_adapter_and_passes_barriers_to_harness(self):
        from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser

        task, gate, events = make_task(), make_gate(), queue.Queue()
        with tempfile.TemporaryDirectory() as temporary:
            args = cli_args(Path(temporary))
            router = Mock()
            router.capabilities.model_call_accounting = "none"
            router.router_id = "test"
            router.route.return_value = SimpleNamespace(
                decision={"status": "selected", "route": "CUA"}, usage=SimpleNamespace(model_calls=0))
            with (fake_harness_modules(), patch("reflexmesh.runtime.cli.claim_fixture", return_value={}),
                  patch("reflexmesh.runtime.cli.importlib.util.find_spec", return_value=object()),
                  patch("reflexmesh.runtime.cli.HarnessStrategy") as harness):
                BrowserExecutionStrategy(task, args, [{"action": "finish"}], router=router,
                                         faults={"hold_decision": {"at": 2, "seconds": 600}})(gate, events)
            factory = harness.call_args.args[1]
            self.assertIs(factory.func, FixtureBrowser)
            barriers = harness.call_args.kwargs["faults"]
            self.assertIs(barriers.gate, gate)
            self.assertIs(barriers.events, events)
            self.assertEqual(barriers.control_dir, Path(temporary))


@unittest.skipUnless(sys.platform != "win32", "supervised fixture runtime uses POSIX fork")
class SupervisedHeldDecision(unittest.TestCase):
    def test_a08_reaches_hold_then_cancel_without_late_dispatch(self):
        """Real runtime supervision with a fake provider; not a browser acceptance run."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "fault-hold-started"
            task = make_task()

            def worker(gate, events):
                env = ControlledEnvironment(FakeEnvironment(), gate, events, lambda *_: None)
                barriers = FaultBarriers({"hold_decision": {"at": 2, "seconds": 600}}, gate, events,
                                         control_dir=directory)
                provider = CountingProvider(Mock(), gate, events, model=True, faults=barriers)
                env.observe()
                provider.decide({}, {})
                env.observe()
                provider.decide({}, {})
                env.execute("click", {"element": "1"})  # Unreachable after cancellation.
                return WorkerResult("finish")

            supervisor = AttemptSupervisor(task, worker)
            accepted = []

            def cancel_when_held():
                stop = time.monotonic() + 3
                while time.monotonic() < stop and not marker.is_file():
                    time.sleep(0.01)
                accepted.append((marker.is_file(), supervisor.cancel()))

            thread = threading.Thread(target=cancel_when_held, daemon=True)
            thread.start()
            try:
                with finite_watchdog(12):
                    result = supervisor.run()
            finally:
                thread.join(timeout=4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(accepted, [(True, True)])
            self.assertEqual((result["attempt_status"], result["stop_reason"]),
                             ("cancelled", "cancel_requested"))
            self.assertEqual(result["budget"]["dispatches"], 0)
            self.assertEqual(result["budget"]["local_model_calls"], 2)
            self.assertEqual(result["task_outcome"], "unknown")
            self.assertEqual(result["cleanup"], "forced")
            self.assertIn(["hold_decision", "started"],
                          [row["data"] for row in result["trace"] if row["kind"] == "fault"])
            self.assertNotEqual(result["attempt_status"], "completed")


if __name__ == "__main__":
    unittest.main()
