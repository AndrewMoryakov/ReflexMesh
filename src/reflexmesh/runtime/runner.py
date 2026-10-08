"""Bounded V0.5 attempt supervisor; adapters supply the action loop and verifier."""

from __future__ import annotations

import multiprocessing as mp
import inspect
import queue
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.process_group import OwnedWorker
from reflexmesh.runtime.ownership import FixtureOwnership, OwnershipError
from reflexmesh.runtime.profile_owner import AttemptProfiles
from reflexmesh.text.slots import SlotRegistry
from reflexmesh.tracing.slot_evidence import SlotEvidence
from reflexmesh.verification.constraints import ConstraintAssessments, assess_constraints

GRACE_SECONDS = 5.0
KILL_SECONDS = 2.0
VERIFIER_POLL_SECONDS = 0.05
VERIFIER_STOP_SECONDS = 0.25
PROFILE_CLEANUP_SECONDS = 2.0
STOP_CODES = {"cancel_requested": "cancelled", "deadline": "incomplete",
              "step_limit": "incomplete", "model_call_limit": "incomplete",
              "effect_unknown": "incomplete",
              "policy_denied": "blocked", "invalid_action": "blocked",
              "adapter_contract_unsupported": "blocked", "no_admissible_action": "blocked"}


class RuntimeStop(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class WorkerResult:
    status: str  # finish | no_confident_action | protocol_error | executor_error
    reason: str = ""


class SkippedAction:
    """Rejected stale proposal; SOH must observe again before choosing an action."""
    ok = False
    text = "Target changed; observe again."
    artifacts = ()
    terminal = False

    def to_dict(self):
        return {"ok": False, "text": self.text, "artifacts": [], "terminal": False, "fields": {}}


class AttemptGate:
    """Small shared state; no browser/provider call may run under the lock."""

    def __init__(self, ctx, task: ExecutionTask, started: float, *,
                 slot_registry=None, slot_sink=None, fixture_owner=None, profile_client=None):
        self.lock = ctx.Lock()
        self.terminal = ctx.Value("i", 0, lock=False)
        self.dispatches = ctx.Value("q", 0, lock=False)
        self.in_flight = ctx.Value("q", 0, lock=False)
        self.steps = ctx.Value("q", 0, lock=False)
        self.calls = ctx.Value("q", 0, lock=False)
        self.opaque_model_usage = ctx.Value("i", 0, lock=False)
        self.router_invocations = ctx.Value("q", 0, lock=False)
        self.generation = ctx.Value("q", 0, lock=False)
        self.sealed_generation = ctx.Value("q", -1, lock=False)
        self.text_dispatches = ctx.Value("q", 0, lock=False)
        self.nontext_dispatches = ctx.Value("q", 0, lock=False)
        self.unknown_dispatches = ctx.Value("q", 0, lock=False)
        self.slot_registry = slot_registry if slot_registry is not None else SlotRegistry.from_task(task)
        self.slot_sink = slot_sink
        self.fixture_owner = fixture_owner
        self.profile_client = profile_client
        self.started = started
        self.deadline = (None if task.limits.wall_seconds is None else
                         started + task.limits.wall_seconds)
        self.max_steps = task.limits.max_steps
        self.max_calls = task.limits.max_model_calls

    def _acquire(self):
        if not self.lock.acquire(timeout=0.25):
            raise RuntimeStop("gate_unavailable")

    def _check(self):
        if self.terminal.value:
            raise RuntimeStop({1: "cancel_requested", 2: "deadline", 3: "terminal"}[self.terminal.value])
        if self.deadline_expired():
            self.terminal.value = 2
            raise RuntimeStop("deadline")

    def stop(self, reason: str) -> bool:
        self._acquire()
        try:
            if self.terminal.value or self.deadline_expired():
                if not self.terminal.value:
                    self.terminal.value = 2
                return False
            self.terminal.value = {"cancel_requested": 1, "deadline": 2}.get(reason, 3)
            return True
        finally:
            self.lock.release()

    def finish(self, *, expected_generation: int | None = None) -> int:
        """Serialize verified completion against cancellation and deadline acceptance."""
        self._acquire()
        try:
            if not self.terminal.value:
                if self.deadline_expired():
                    self.terminal.value = 2
                elif expected_generation is not None and (
                        self.generation.value != expected_generation or
                        self.sealed_generation.value != expected_generation or self.in_flight.value):
                    raise RuntimeStop("evidence_changed")
                else:
                    self.terminal.value = 3
            return self.terminal.value
        finally:
            self.lock.release()

    def reserve_step(self):
        self._acquire()
        try:
            self._check()
            if self.max_steps is not None and self.steps.value >= self.max_steps:
                raise RuntimeStop("step_limit")
            self.steps.value += 1
        finally:
            self.lock.release()

    def reserve_call(self):
        """Reserve one locally controlled provider request, before sending it."""
        self._acquire()
        try:
            self._check()
            if self.max_calls is not None and self.calls.value >= self.max_calls:
                raise RuntimeStop("model_call_limit")
            self.calls.value += 1
        finally:
            self.lock.release()

    def begin_routing(self, accounting: str):
        """Persist accounting uncertainty before routing, even if its worker dies."""
        if accounting not in ("none", "local", "opaque"):
            raise RuntimeStop("adapter_contract_unsupported")
        self._acquire()
        try:
            self._check()
            self.router_invocations.value += 1
            if accounting == "opaque":
                self.opaque_model_usage.value = 1
        finally:
            self.lock.release()

    def deadline_expired(self, now: float | None = None) -> bool:
        return self.deadline is not None and (time.monotonic() if now is None else now) >= self.deadline

    def wall_remaining(self, now: float | None = None) -> float | None:
        """Read the allowance, including after terminal acceptance; None is unlimited."""
        return (None if self.deadline is None else
                max(0.0, self.deadline - (time.monotonic() if now is None else now)))

    def remaining(self) -> float | None:
        self._acquire()
        try:
            self._check()
            return self.wall_remaining()
        finally:
            self.lock.release()

    def operation_timeout(self, maximum: float) -> float:
        """Bound an individual I/O operation without imposing a whole-task deadline."""
        remaining = self.remaining()
        return maximum if remaining is None else min(maximum, remaining)

    def commit_dispatch(self, classification: str = "unknown") -> int:
        self._acquire()
        try:
            self._check()
            if self.sealed_generation.value >= 0:
                raise RuntimeStop("dispatches_sealed")
            if not self.steps.value:
                raise RuntimeStop("step_limit")
            if self.in_flight.value:
                raise RuntimeStop("effect_unknown")
            self.dispatches.value += 1
            self.generation.value += 1
            counter = {"text": self.text_dispatches, "nontext": self.nontext_dispatches}.get(
                classification, self.unknown_dispatches)
            counter.value += 1
            self.in_flight.value = self.dispatches.value
            return self.dispatches.value
        finally:
            self.lock.release()

    def seal_dispatches(self) -> dict:
        """Close coverage without accepting completion or excluding cancellation.

        Existing calls can still return. No browser, IPC, or evidence operation
        runs while this bounded lock is held.
        """
        self._acquire()
        try:
            self.sealed_generation.value = self.generation.value
            return self._slot_snapshot_unlocked()
        finally:
            self.lock.release()

    def _slot_snapshot_unlocked(self) -> dict:
        return {"dispatches": self.dispatches.value, "generation": self.generation.value,
                "sealed_generation": self.sealed_generation.value,
                "text_dispatches": self.text_dispatches.value,
                "nontext_dispatches": self.nontext_dispatches.value,
                "unknown_dispatches": self.unknown_dispatches.value}

    def slot_snapshot(self) -> dict:
        self._acquire()
        try:
            return self._slot_snapshot_unlocked()
        finally:
            self.lock.release()

    def settle(self, action_id: int) -> None:
        self._acquire()
        try:
            if self.in_flight.value == action_id:
                self.in_flight.value = 0
        finally:
            self.lock.release()

    def snapshot(self):
        self._acquire()
        try:
            return {"steps": self.steps.value, "model_calls": self.calls.value,
                    "opaque_model_usage": bool(self.opaque_model_usage.value),
                    "router_invocations": self.router_invocations.value,
                    "dispatches": self.dispatches.value, "in_flight": self.in_flight.value,
                    "terminal": self.terminal.value}
        finally:
            self.lock.release()


class ControlledEnvironment:
    """Compatible with SystemOneHarness Environment; policy and target checks are injected."""

    def __init__(self, inner, gate: AttemptGate, events,
                 admit: Callable[[str, dict], str | None], *, bootstrap: bool = False):
        # This enrollment is deliberately narrow. A duck-typed/fake environment
        # cannot turn its own assertions into trusted boundary coverage.
        from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser

        self.inner, self.gate, self.events, self.admit = inner, gate, events, admit
        self._prepared_protocol = type(inner) is FixtureBrowser
        if self._prepared_protocol:
            inner.bind_profile_owner(gate.profile_client)
            inner.bind_ownership(gate.fixture_owner)
            inner.bind_runtime(gate.slot_registry, gate.slot_sink)
        self.bootstrap = bootstrap
        self.last_stop: str | None = None
        self.seen_mutations: set[tuple] = set()
        self.uncertain_mutation = False

    def reset(self, goal):
        if not self.bootstrap:
            self.inner.reset(goal)
            return
        self.gate.reserve_step()
        self.events.put(("proposal", {"action": "navigate"}))
        reason = self.admit("navigate", {"bootstrap": True})
        if reason:
            self.last_stop = reason
            raise RuntimeStop(reason)
        action_id = self._commit("nontext" if self._prepared_protocol else "unknown")
        self.events.put(("dispatch", action_id, {"operation": "navigate", "bootstrap": True,
                                                **self._ownership_descriptor()}))
        try:
            self.inner.reset(goal)
        except Exception:
            self.events.put(("effect_unknown", action_id))
            raise
        else:
            self.events.put(("driver_return", action_id, True))
        finally:
            self.gate.settle(action_id)

    def observe(self):
        try:
            self.gate.reserve_step()
        except RuntimeStop as exc:
            self.last_stop = exc.reason
            raise
        obs = self.inner.observe()
        self._record_observation(obs)
        return obs

    def verification_observation(self):
        """Fresh read-only snapshot after the action loop; no new decision cycle."""
        self.gate._acquire()
        try:
            self.gate._check()
        finally:
            self.gate.lock.release()
        obs = self.inner.observe()
        self._record_observation(obs)
        return obs

    def _record_observation(self, obs):
        safe = {k: obs.fields[k] for k in ("url", "run_id", "target_ids", "ownership")
                if hasattr(obs, "fields") and k in obs.fields}
        if safe:
            safe["captured_at"] = time.time()
            self.events.put(("observation", safe))

    def execute(self, action, params):
        # Never copy free-form provider parameters into the public trace.
        self.events.put(("proposal", {"action": action}))
        prepared = None
        if self._prepared_protocol:
            try:
                prepared = self.inner.prepare(action, params)
                # Preserve an explicitly injected additional policy. It sees a
                # fresh copy of the admitted parameters and cannot mutate the
                # private command that will actually be dispatched.
                reason = (self.admit(prepared.action, dict(prepared._parameters))
                          if self.admit != self.inner.admit else None)
            except RuntimeStop as exc:
                reason = exc.reason
        else:
            reason = self.admit(action, params)
        if reason == "stale_target":
            self.events.put(("reobserve", "stale_target"))
            return SkippedAction()
        if reason:
            self.last_stop = reason
            self.events.put(("rejected", reason))
            raise RuntimeStop(reason)
        descriptor = (prepared.descriptor() if prepared is not None else
                      self.inner.describe(action, params) if hasattr(self.inner, "describe") else
                      {"operation": action})
        operation_key = (descriptor.get("operation"), descriptor.get("target_id"), descriptor.get("slot_ref"))
        mutating = descriptor.get("operation") != "navigate"
        if mutating and self.uncertain_mutation:
            self.last_stop = "effect_unknown"
            self.events.put(("rejected", "effect_unknown"))
            raise RuntimeStop("effect_unknown")
        if mutating and operation_key in self.seen_mutations:
            self.last_stop = "invalid_action"
            self.events.put(("rejected", "invalid_action"))
            raise RuntimeStop("invalid_action")
        try:
            classification = ("text" if prepared.action == "type_text" else "nontext") if prepared else "unknown"
            action_id = self._commit(classification, descriptor.get("slot_ref"))
        except RuntimeStop as exc:
            self.last_stop = exc.reason
            raise
        if mutating:
            self.seen_mutations.add(operation_key)
        self.events.put(("dispatch", action_id, {**descriptor, **self._ownership_descriptor()}))
        try:
            result = (self.inner.execute_prepared(prepared, action_id) if prepared is not None else
                      self.inner.execute(action, params))
        except Exception:
            # Driver exceptions do not prove the external effect did not happen.
            if mutating:
                self.uncertain_mutation = True
            self.events.put(("effect_unknown", action_id))
            raise
        else:
            if mutating and not result.ok:
                self.uncertain_mutation = True
            self.events.put(("driver_return", action_id, bool(result.ok)))
            return result
        finally:
            self.gate.settle(action_id)

    def _commit(self, classification: str, slot_ref: str | None = None) -> int:
        # Shared obligation counts change at commit, before any fallible IPC.
        # A killed worker cannot erase a committed action by losing its event.
        action_id = self.gate.commit_dispatch(classification)
        if self.gate.slot_sink is not None:
            self.gate.slot_sink.commit(action_id, classification, slot_ref)
        return action_id

    def _ownership_descriptor(self) -> dict:
        owner = self.gate.fixture_owner
        return {"ownership": owner.binding} if self._prepared_protocol and type(owner) is FixtureOwnership else {}

    def close(self):
        self.inner.close()


def _work(strategy, gate, events):
    try:
        result = strategy(gate, events)
        if not isinstance(result, WorkerResult):
            raise TypeError("worker must return WorkerResult")
        events.put(("done", result.status, result.reason))
    except RuntimeStop as exc:
        events.put(("stopped", exc.reason))
    except BaseException:
        events.put(("done", "executor_error", "worker_exception"))
    finally:
        # Raw-fork ownership deliberately bypasses multiprocessing's implicit
        # child reaper; flush this worker's queue before os._exit instead.
        events.close()
        events.join_thread()


def _verification_work(verifier, task, timeout, observation, result_queue):
    try:
        parameters = inspect.signature(verifier).parameters
        result_queue.put(verifier(task, timeout, observation) if len(parameters) >= 3
                         else verifier(task, timeout))
    except BaseException:
        result_queue.put(None)


class AttemptSupervisor:
    """Supervisor process retains ownership after a worker is stopped or terminated."""

    def __init__(self, task: ExecutionTask, strategy, verifier=None, *, verifier_factory=None):
        self.task, self.strategy, self.verifier = task, strategy, verifier
        self.verifier_factory = verifier_factory
        self.context = mp.get_context("fork")
        self.started = time.monotonic()
        self.attempt_id = uuid.uuid4().hex
        self.fixture_owner = FixtureOwnership(self.context, task, self.attempt_id)
        self.profiles = AttemptProfiles(self.context)
        registry = SlotRegistry.from_task(task)
        self.slot_evidence = SlotEvidence(self.context, task, self.attempt_id, registry=registry)
        self.gate = AttemptGate(self.context, task, self.started,
                                slot_registry=registry, slot_sink=self.slot_evidence.sink,
                                fixture_owner=self.fixture_owner, profile_client=self.profiles.client)
        self.fixture_owner.bind_gate(self.gate)
        self.profiles.client.bind_gate(self.gate)
        # This lock-free high-water mark can reject foreign action IDs and
        # retain actual failures after lock death; it can never establish pass.
        self.slot_evidence.bind_dispatch_counter(self.gate.dispatches)
        self.events = self.context.Queue()
        self.process = OwnedWorker(self.context, _work, (strategy, self.gate, self.events))
        self._cancel_requested = False
        self.observation = None
        self.trace = []
        self.routing = None
        self.router_capabilities = None
        self.router_usage = None
        self.executor_id = None
        self._constraints = ConstraintAssessments()
        self._assessment_generation = None
        self._fixture_fence_attempted = False

    def _assess_constraints(self, snapshot=None, *, retain_passes=False, read_budget=True):
        if snapshot is None and read_budget:
            try:
                snapshot = self.gate.snapshot()
            except RuntimeStop:
                # Unlocked fallback counters cannot establish a passing assessment.
                pass
        try:
            coverage = self.gate.slot_snapshot()
        except RuntimeStop:
            coverage = None
        self._assessment_generation = (coverage["generation"] if coverage is not None and
                                       coverage["sealed_generation"] == coverage["generation"] else None)
        self._constraints.update(assess_constraints(self.task, snapshot, self.attempt_id,
                                                   slot_evidence=self.slot_evidence, coverage=coverage,
                                                   ownership=self.fixture_owner),
                                 retain_passes=retain_passes)

    def cancel(self):
        try:
            accepted = self.gate.stop("cancel_requested")
        except RuntimeStop:
            return False
        self._cancel_requested = accepted or self.gate.terminal.value == 1
        return accepted

    def _unknown_verification(self) -> list[dict]:
        return [{"id": c.id, "kind": "postcondition", "status": "unknown",
                 "observed_at": None, "evidence_refs": []} for c in self.task.criteria]

    def _verify(self, remaining: float | None, *, interruptible: bool = False) -> list[dict]:
        unknown = self._unknown_verification()
        if not self.verifier or (remaining is not None and remaining <= 0):
            return unknown
        deadline = None if remaining is None else time.monotonic() + remaining
        if interruptible:
            if self.gate.deadline is not None:
                deadline = (self.gate.deadline if deadline is None else
                            min(deadline, self.gate.deadline))
            if self.gate.terminal.value in (1, 2):
                return unknown
        results = self.context.Queue()
        verifier = self.context.Process(target=_verification_work,
                                        args=(self.verifier, self.task, remaining, self.observation, results))
        verifier.start()
        value = None
        try:
            while True:
                self.slot_evidence.drain()
                # Completion verification must notice an accepted cancel even if
                # the verifier never returns. Post-terminal evidence retains its
                # separate, bounded grace window.
                if interruptible and self.gate.terminal.value in (1, 2):
                    break
                wait = None if deadline is None else deadline - time.monotonic()
                if wait is not None and wait <= 0:
                    break
                try:
                    value = results.get(timeout=VERIFIER_POLL_SECONDS if wait is None else
                                        min(VERIFIER_POLL_SECONDS, wait))
                    break
                except queue.Empty:
                    if not verifier.is_alive():
                        # It may have published and exited between the timed
                        # read and the liveness check. Normal exit flushes IPC.
                        try:
                            value = results.get_nowait()
                        except (queue.Empty, EOFError):
                            pass
                        break
                    continue
                except EOFError:
                    break
        finally:
            if verifier.is_alive():
                verifier.terminate()
            verifier.join(timeout=VERIFIER_STOP_SECONDS)
            if verifier.is_alive():
                verifier.kill()
                verifier.join(timeout=VERIFIER_STOP_SECONDS)
            results.close()
        if interruptible and self.gate.terminal.value == 1:
            return unknown
        if (type(value) is not list or len(value) != len(self.task.criteria) or
                any(type(c) is not dict or c.get("status") not in ("pass", "fail", "unknown") or
                    c.get("id") != expected.id for c, expected in zip(value, self.task.criteria))):
            return unknown
        return [{**row, "kind": "postcondition", "observed_at": row.get("observed_at")}
                for row in value]

    def _record(self, event, actions):
        if event[0] in ("dispatch", "driver_return", "effect_unknown", "rejected", "reobserve",
                        "decision_request", "step", "stopped", "done", "observation", "proposal"):
            self.trace.append({"seq": len(self.trace) + 1, "kind": event[0], "data": list(event[1:])})
        seq = len(self.trace)
        if event[0] == "fixture_claim":
            try:
                self.fixture_owner.adopt(event[1])
                from reflexmesh.verification.verifier import FixtureVerifier

                if self.verifier_factory is FixtureVerifier and self.verifier is None:
                    self.verifier = FixtureVerifier(self.task, event[1]["baseline"],
                                                    ownership=self.fixture_owner)
            except (OwnershipError, ValueError, KeyError, TypeError):
                # A lost or malformed claim receipt cannot install a verifier.
                # The shared attempted marker still requires terminal fencing.
                pass
        elif event[0] == "baseline":
            if self.verifier_factory and self.verifier is None:
                self.verifier = self.verifier_factory(self.task, event[1])
        elif event[0] == "routing":
            self.routing = event[1]
        elif event[0] == "router_capabilities":
            self.router_capabilities = {"router_id": event[1], "model_call_accounting": event[2]}
        elif event[0] == "router_usage":
            self.router_usage = {"model_calls": event[1]}
        elif event[0] == "executor_selected":
            self.executor_id = event[1]
        elif event[0] == "proposal":
            self._last_proposal_seq = seq
        elif event[0] == "dispatch":
            actions.append({"id": event[1], **event[2], "effect": "unknown",
                            "proposal_seq": getattr(self, "_last_proposal_seq", None),
                            "dispatch_seq": seq, "return_seq": None, "evidence_refs": []})
        elif event[0] == "driver_return":
            for row in actions:
                if row["id"] == event[1]:
                    row["driver_returned"] = True
                    row["return_seq"] = seq
        elif event[0] == "observation":
            self.observation = event[1]
            self._observation_seq = seq
            self._reconcile_navigation(actions)

    def _reconcile_navigation(self, actions):
        observation = self.observation
        if observation is None:
            return
        for row in actions:
            if (row.get("operation") == "navigate" and row.get("driver_returned") and
                    row.get("effect") == "unknown" and
                    type(row.get("return_seq")) is int and
                    self._observation_seq > row["return_seq"] > row.get("dispatch_seq", -1) and
                    observation.get("url") == row.get("destination", self.task.origin + self.task.start_path) and
                    self._same_owner(row.get("ownership"), observation.get("ownership"),
                                     bootstrap=row.get("bootstrap") is True and row.get("id") == 1)):
                row["effect"] = "applied"
                row["evidence_refs"] = [f"browser:{self.attempt_id}:{self._observation_seq}"]

    def _reconcile(self, actions, verification):
        covered = {"form_submitted_once": {"submit_form", "type_text"},
                   "settings_saved": {"save_settings", "toggle_setting"},
                   "export_completed_once": {"start_export"}}
        validated = set()
        for row in actions:
            matches = [assessment for criterion, assessment in zip(self.task.criteria, verification)
                       if assessment["status"] == "pass" and
                       row.get("operation") in assessment.get("verified_operations", covered.get(criterion.predicate, set())) and
                       self._same_owner(row.get("ownership"), assessment.get("ownership"))]
            if matches:
                row["effect"] = "applied"
                row["evidence_refs"] = list(dict.fromkeys(ref for assessment in matches
                                                         for ref in assessment.get("evidence_refs", [])))
                validated.add(row.get("operation"))
        return validated

    def _same_owner(self, action_binding, evidence_binding, *, bootstrap=False):
        # Generic strategies without a fixture claim retain their synthetic test
        # contract. The production browser path always has a claim marker.
        if not self.fixture_owner.acquisition_attempted.value:
            return action_binding is None and evidence_binding is None
        expected = self.fixture_owner.verified_binding
        if (type(expected) is not dict or not {"session_id", "profile_id"} <= set(expected) or
                type(action_binding) is not dict or type(evidence_binding) is not dict or
                set(evidence_binding) != set(expected) or
                any(type(evidence_binding[k]) is not type(value) or evidence_binding[k] != value
                    for k, value in expected.items())):
            return False
        action_expected = {key: value for key, value in expected.items()
                           if not bootstrap or key not in ("session_id", "profile_id")}
        return (set(action_binding) == set(action_expected) and
                all(type(action_binding[k]) is type(value) and action_binding[k] == value
                    for k, value in action_expected.items()))

    def _fence_fixture(self):
        if self.fixture_owner.acquisition_attempted.value and not self._fixture_fence_attempted:
            self._fixture_fence_attempted = True
            try:
                self.fixture_owner.revoke(timeout=0.25)
            except (OwnershipError, OSError, ValueError):
                pass  # The server never expires or reassigns an uncertain claim.

    def run(self) -> dict:
        status = reason = None
        actions: list[dict] = []
        terminal_at = None
        self.process.start()
        while status is None:
            self.slot_evidence.drain()
            self.profiles.service()
            now = time.monotonic()
            if self._cancel_requested or self.gate.terminal.value == 1:
                status, reason = "cancelled", "cancel_requested"
            elif self.gate.deadline_expired(now) or self.gate.terminal.value == 2:
                try:
                    self.gate.stop("deadline")
                except RuntimeStop:
                    pass
                status, reason = "incomplete", "deadline"
            else:
                try:
                    remaining = self.gate.wall_remaining(now)
                    event = self.events.get(timeout=0.05 if remaining is None else
                                            min(0.05, max(0.001, remaining)))
                except queue.Empty:
                    if not self.process.is_alive():
                        status, reason = "failed", "executor_error"
                    continue
                if event[0] in ("dispatch", "driver_return", "observation"):
                    self._record(event, actions)
                elif event[0] == "effect_unknown":
                    self._record(event, actions)
                elif event[0] == "rejected":
                    self._record(event, actions)
                    status, reason = STOP_CODES.get(event[1], "blocked"), event[1]
                elif event[0] == "stopped":
                    self._record(event, actions)
                    reason = event[1]
                    status = STOP_CODES.get(reason, "failed")
                elif event[0] == "done":
                    self._record(event, actions)
                    kind = event[1]
                    if kind in ("finish", "no_confident_action"):
                        status, reason = "verifying", kind
                    elif kind == "blocked":
                        status, reason = "blocked", event[2]
                    else:
                        status, reason = "failed", ("protocol_error" if kind == "protocol_error" else "executor_error")
                else:
                    self._record(event, actions)
        terminal_at = time.monotonic()
        self.profiles.close_requests()
        if status != "verifying":
            self._fence_fixture()
        try:
            self.gate.seal_dispatches()
        except RuntimeStop:
            # Coverage remains unknown. Cancellation and cleanup still use their
            # ordinary bounded paths even if a dead worker held the gate lock.
            pass
        self._assess_constraints()
        if status != "verifying":
            try:
                terminal_gate = self.gate.finish()
            except RuntimeStop:
                status, reason = "failed", "executor_error"
            else:
                if terminal_gate == 1:
                    status, reason = "cancelled", "cancel_requested"
                elif terminal_gate == 2:
                    status, reason = "incomplete", "deadline"
        verification = (self._verify(self.gate.wall_remaining(terminal_at), interruptible=True)
                        if status == "verifying" else None)
        if status == "verifying":
            assessments = [c["status"] for c in verification]
            # Evaluate evidence on private rows until finish() serializes the
            # decision against cancellation and the original wall deadline.
            verified_actions = [dict(row) for row in actions]
            validated = self._reconcile(verified_actions, verification)
            unknown_mutations = any(row.get("operation") not in validated and
                                    row.get("operation") != "navigate" for row in verified_actions)
            self._assess_constraints()
            if (assessments and all(v == "pass" for v in assessments) and
                    self._constraints.all_pass() and
                    self._assessment_generation is not None and
                    not self.gate.in_flight.value and len(actions) == self.gate.dispatches.value and
                    not unknown_mutations and all(row["effect"] != "unknown" for row in verified_actions)):
                try:
                    accepted = self.gate.finish(expected_generation=self._assessment_generation)
                except RuntimeStop:
                    status, reason = "incomplete", "verification_unknown"
                else:
                    status, reason = ({3: ("completed", "verified"), 1: ("cancelled", "cancel_requested"),
                                       2: ("incomplete", "deadline")})[accepted]
                    if (status == "completed" and len(actions) == 1 and
                            actions[0].get("operation") == "navigate" and
                            any(c.predicate == "current_page" for c in self.task.criteria)):
                        reason = "already_satisfied"
            elif self._constraints.has_fail():
                status, reason = "failed", "constraint_violated"
            elif "fail" in assessments:
                status, reason = "failed", "postcondition_failed"
            else:
                status, reason = "incomplete", "verification_unknown"
            if status not in ("completed", "cancelled"):
                try:
                    terminal_gate = self.gate.finish()
                except RuntimeStop:
                    status, reason = "failed", "executor_error"
                else:
                    if terminal_gate == 1:
                        status, reason = "cancelled", "cancel_requested"
                    elif terminal_gate == 2:
                        status, reason = "incomplete", "deadline"
            if status == "cancelled":
                # A pass arriving after an accepted cancel must not refine the
                # cancelled completion attempt, including action effects.
                verification = self._unknown_verification()
        try:
            self.gate.stop("terminal")
        except RuntimeStop:
            pass
        self._fence_fixture()
        grace_end = terminal_at + GRACE_SECONDS
        while self.process.is_alive() and time.monotonic() < grace_end:
            self.slot_evidence.drain()
            try:
                self._record(self.events.get(timeout=min(0.05, max(0.001, grace_end - time.monotonic()))), actions)
            except queue.Empty:
                pass
        # The owned group can outlive its leader. Keep its PID reserved until
        # TERM/KILL and live-member verification finish, independent of is_alive.
        cleanup = self.process.cleanup(KILL_SECONDS)
        worker_cleanup = cleanup
        if verification is None:
            verification = self._verify(max(0, grace_end - time.monotonic()))
        while True:
            try:
                self._record(self.events.get_nowait(), actions)
            except queue.Empty:
                break
        # Late evidence can refine an effect, but the accepted terminal status never changes.
        self._reconcile_navigation(actions)
        self._reconcile(actions, verification)
        synchronized_snapshot = True
        try:
            snapshots = self.gate.snapshot()
        except RuntimeStop:
            synchronized_snapshot = False
            snapshots = {"steps": self.gate.steps.value, "model_calls": self.gate.calls.value,
                         "opaque_model_usage": bool(self.gate.opaque_model_usage.value),
                         "router_invocations": self.gate.router_invocations.value,
                         "dispatches": self.gate.dispatches.value, "in_flight": self.gate.in_flight.value}
            cleanup = "unknown"
            self._assess_constraints(retain_passes=status == "completed", read_budget=False)
        else:
            self._assess_constraints(snapshots, retain_passes=status == "completed")
        # Gate evidence can be unavailable after lock death even when process
        # absence is already proved. Use that actual cleanup proof for deletion;
        # preserve aggregate uncertainty independently for fixture release.
        profile_cleanup = self.profiles.cleanup(worker_cleanup, PROFILE_CLEANUP_SECONDS)
        if profile_cleanup["status"] not in ("not_created", "removed"):
            # Process absence alone does not establish complete resource cleanup.
            # Keep the fixture fenced if its owned profile could not be removed.
            cleanup = "unknown"
        if self.fixture_owner.acquisition_attempted.value:
            try:
                # Only source-backed absence of the owned group allows reuse.
                # Unknown cleanup leaves the claim quarantined indefinitely.
                if cleanup in ("closed", "forced"):
                    self.fixture_owner.release(cleanup, timeout=0.25)
            except (OwnershipError, OSError, ValueError):
                pass
        if self.fixture_owner.acquisition_attempted.value:
            self._assess_constraints(snapshots if synchronized_snapshot else None,
                                     retain_passes=status == "completed", read_budget=synchronized_snapshot)
        # A child can commit dispatch and die before the event reaches the parent.
        if snapshots["dispatches"] > len(actions):
            actions.extend({"id": i, "effect": "unknown"} for i in range(len(actions) + 1, snapshots["dispatches"] + 1))
        limits = self.task.limits
        verification.extend(self._constraints.rows())
        # Late confirmed failures refine the outcome, never the accepted stop.
        # A completed attempt retains its accepted evidence when a later read is
        # missing, but cannot hide a newly confirmed violation behind that pass.
        outcome = ("fail" if self._constraints.has_fail() else
                   "pass" if status == "completed" and self._constraints.all_pass() else
                   "fail" if status == "failed" and
                   reason in ("postcondition_failed", "constraint_violated") else
                   "unknown")
        elapsed = round(time.monotonic() - self.started, 3)
        slot_records = self.slot_evidence.export()
        self.slot_evidence.close()
        return {"schema_version": "execution-result/0.1", "task_id": self.task.task_id,
                "revision": self.task.revision, "attempt_id": self.attempt_id,
                "executor_id": self.executor_id, "routing": self.routing,
                "router_capabilities": self.router_capabilities, "router_usage": self.router_usage,
                "attempt_status": status, "stop_reason": reason, "task_outcome": outcome,
                "execution_outcome": "unknown" if snapshots["in_flight"] or any(
                    a["effect"] == "unknown" for a in actions) else
                    "returned" if snapshots["dispatches"] else
                    "error" if status == "failed" else "not_started",
                "verification": verification, "actions": actions, "cleanup": cleanup,
                "worker_cleanup": worker_cleanup, "profile_cleanup": profile_cleanup,
                "trace": self.trace,
                "slot_evidence": slot_records,
                "ownership_evidence": self.fixture_owner.export(),
                "budget": {"steps": snapshots["steps"],
                           "model_calls": None if snapshots["opaque_model_usage"] else snapshots["model_calls"],
                           "local_model_calls": snapshots["model_calls"],
                           "model_calls_reason": "router_internal_usage_unknown" if snapshots["opaque_model_usage"] else None,
                           "model_call_limit_scope": "locally_controlled_provider_requests",
                           "router_invocations": snapshots["router_invocations"],
                           "dispatches": snapshots["dispatches"],
                           "limits": {"wall_seconds": limits.wall_seconds, "max_steps": limits.max_steps,
                                      "max_model_calls": limits.max_model_calls},
                           "remaining_steps": (None if limits.max_steps is None else
                                               max(0, limits.max_steps - snapshots["steps"])),
                           "remaining_model_calls": (None if limits.max_model_calls is None else
                                                     max(0, limits.max_model_calls - snapshots["model_calls"])),
                           "remaining_wall_seconds": (None if self.gate.deadline is None else
                                                      round(self.gate.wall_remaining(), 3)),
                           "elapsed_seconds": elapsed, "cost": None, "cost_reason": "not_measured"}}
