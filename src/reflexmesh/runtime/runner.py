"""Bounded V0.5 attempt supervisor; adapters supply the action loop and verifier."""

from __future__ import annotations

import multiprocessing as mp
import inspect
import os
import queue
import signal
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from reflexmesh.contracts.execution import ExecutionTask

GRACE_SECONDS = 5.0
KILL_SECONDS = 2.0
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

    def __init__(self, ctx, task: ExecutionTask, started: float):
        self.lock = ctx.Lock()
        self.terminal = ctx.Value("i", 0, lock=False)
        self.dispatches = ctx.Value("i", 0, lock=False)
        self.in_flight = ctx.Value("i", 0, lock=False)
        self.steps = ctx.Value("i", 0, lock=False)
        self.calls = ctx.Value("i", 0, lock=False)
        self.started = started
        self.deadline = started + task.limits.wall_seconds
        self.max_steps = task.limits.max_steps
        self.max_calls = task.limits.max_model_calls

    def _acquire(self):
        if not self.lock.acquire(timeout=0.25):
            raise RuntimeStop("gate_unavailable")

    def _check(self):
        if self.terminal.value:
            raise RuntimeStop({1: "cancel_requested", 2: "deadline", 3: "terminal"}[self.terminal.value])
        if time.monotonic() >= self.deadline:
            self.terminal.value = 2
            raise RuntimeStop("deadline")

    def stop(self, reason: str) -> bool:
        self._acquire()
        try:
            if self.terminal.value or time.monotonic() >= self.deadline:
                if not self.terminal.value:
                    self.terminal.value = 2
                return False
            self.terminal.value = {"cancel_requested": 1, "deadline": 2}.get(reason, 3)
            return True
        finally:
            self.lock.release()

    def finish(self) -> int:
        """Serialize verified completion against cancellation and deadline acceptance."""
        self._acquire()
        try:
            if not self.terminal.value:
                self.terminal.value = 2 if time.monotonic() >= self.deadline else 3
            return self.terminal.value
        finally:
            self.lock.release()

    def reserve_step(self):
        self._acquire()
        try:
            self._check()
            if self.steps.value >= self.max_steps:
                raise RuntimeStop("step_limit")
            self.steps.value += 1
        finally:
            self.lock.release()

    def reserve_call(self):
        self._acquire()
        try:
            self._check()
            if self.calls.value >= self.max_calls:
                raise RuntimeStop("model_call_limit")
            self.calls.value += 1
        finally:
            self.lock.release()

    def remaining(self) -> float:
        self._acquire()
        try:
            self._check()
            return max(0.0, self.deadline - time.monotonic())
        finally:
            self.lock.release()

    def commit_dispatch(self) -> int:
        self._acquire()
        try:
            self._check()
            if not self.steps.value:
                raise RuntimeStop("step_limit")
            if self.in_flight.value:
                raise RuntimeStop("effect_unknown")
            self.dispatches.value += 1
            self.in_flight.value = self.dispatches.value
            return self.dispatches.value
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
                    "dispatches": self.dispatches.value, "in_flight": self.in_flight.value,
                    "terminal": self.terminal.value}
        finally:
            self.lock.release()


class ControlledEnvironment:
    """Compatible with SystemOneHarness Environment; policy and target checks are injected."""

    def __init__(self, inner, gate: AttemptGate, events,
                 admit: Callable[[str, dict], str | None], *, bootstrap: bool = False):
        self.inner, self.gate, self.events, self.admit = inner, gate, events, admit
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
        action_id = self.gate.commit_dispatch()
        self.events.put(("dispatch", action_id, {"operation": "navigate"}))
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
        safe = {k: obs.fields[k] for k in ("url", "run_id", "target_ids")
                if hasattr(obs, "fields") and k in obs.fields}
        if safe:
            safe["captured_at"] = time.time()
            self.events.put(("observation", safe))

    def execute(self, action, params):
        # Never copy free-form provider parameters into the public trace.
        self.events.put(("proposal", {"action": action}))
        reason = self.admit(action, params)
        if reason == "stale_target":
            self.events.put(("reobserve", "stale_target"))
            return SkippedAction()
        if reason:
            self.last_stop = reason
            self.events.put(("rejected", reason))
            raise RuntimeStop(reason)
        descriptor = self.inner.describe(action, params) if hasattr(self.inner, "describe") else {"operation": action}
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
            action_id = self.gate.commit_dispatch()
        except RuntimeStop as exc:
            self.last_stop = exc.reason
            raise
        if mutating:
            self.seen_mutations.add(operation_key)
        self.events.put(("dispatch", action_id, descriptor))
        try:
            result = self.inner.execute(action, params)
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

    def close(self):
        self.inner.close()


def _work(strategy, gate, events):
    try:
        if hasattr(os, "setsid"):
            os.setsid()  # Own the browser descendants when this worker must be terminated.
        result = strategy(gate, events)
        if not isinstance(result, WorkerResult):
            raise TypeError("worker must return WorkerResult")
        events.put(("done", result.status, result.reason))
    except RuntimeStop as exc:
        events.put(("stopped", exc.reason))
    except BaseException:
        events.put(("done", "executor_error", "worker_exception"))


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
        self.gate = AttemptGate(self.context, task, self.started)
        self.events = self.context.Queue()
        self.process = self.context.Process(target=_work, args=(strategy, self.gate, self.events), daemon=False)
        self.attempt_id = uuid.uuid4().hex
        self._cancel_requested = False
        self.observation = None
        self.trace = []
        self.routing = None
        self.executor_id = None

    def cancel(self):
        try:
            accepted = self.gate.stop("cancel_requested")
        except RuntimeStop:
            return False
        self._cancel_requested = accepted or self.gate.terminal.value == 1
        return accepted

    def _verify(self, remaining: float) -> list[dict]:
        unknown = [{"id": c.id, "kind": "postcondition", "status": "unknown",
                    "observed_at": None, "evidence_refs": []} for c in self.task.criteria]
        if not self.verifier or remaining <= 0:
            return unknown
        results = self.context.Queue()
        verifier = self.context.Process(target=_verification_work,
                                        args=(self.verifier, self.task, remaining, self.observation, results))
        verifier.start()
        try:
            value = results.get(timeout=remaining)
        except (queue.Empty, EOFError):
            value = None
        finally:
            if verifier.is_alive():
                verifier.terminate()
            verifier.join(timeout=0.25)
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
        if event[0] == "baseline":
            if self.verifier_factory and self.verifier is None:
                self.verifier = self.verifier_factory(self.task, event[1])
        elif event[0] == "routing":
            self.routing = event[1]
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
            for row in actions:
                if (row.get("operation") == "navigate" and row.get("driver_returned") and
                        row.get("effect") == "unknown" and
                        event[1].get("url") == row.get("destination", self.task.origin + self.task.start_path)):
                    row["effect"] = "applied"
                    row["evidence_refs"] = [f"browser:{self.attempt_id}:{seq}"]

    def _reconcile(self, actions, verification):
        covered = {"form_submitted_once": {"submit_form", "type_text"},
                   "settings_saved": {"save_settings", "toggle_setting"},
                   "export_completed_once": {"start_export"}}
        validated = set().union(*(covered.get(c.predicate, set())
                                  for c, row in zip(self.task.criteria, verification) if row["status"] == "pass"))
        refs = list(dict.fromkeys(ref for row in verification if row["status"] == "pass"
                                  for ref in row.get("evidence_refs", [])))
        for row in actions:
            if row.get("operation") in validated:
                row["effect"] = "applied"
                row["evidence_refs"] = refs
        return validated

    def run(self) -> dict:
        status = reason = None
        actions: list[dict] = []
        terminal_at = None
        self.process.start()
        while status is None:
            now = time.monotonic()
            if self._cancel_requested or self.gate.terminal.value == 1:
                status, reason = "cancelled", "cancel_requested"
            elif now >= self.gate.deadline or self.gate.terminal.value == 2:
                try:
                    self.gate.stop("deadline")
                except RuntimeStop:
                    pass
                status, reason = "incomplete", "deadline"
            else:
                try:
                    event = self.events.get(timeout=min(0.05, max(0.001, self.gate.deadline - now)))
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
        verification = self._verify(max(0, self.gate.deadline - terminal_at)) if status == "verifying" else None
        if status == "verifying":
            assessments = [c["status"] for c in verification]
            validated = self._reconcile(actions, verification)
            unknown_mutations = any(row.get("operation") not in validated and
                                    row.get("operation") != "navigate" for row in actions)
            if (assessments and all(v == "pass" for v in assessments) and
                    not self.gate.in_flight.value and len(actions) == self.gate.dispatches.value and
                    not unknown_mutations and all(row["effect"] != "unknown" for row in actions)):
                try:
                    accepted = self.gate.finish()
                except RuntimeStop:
                    status, reason = "failed", "executor_error"
                else:
                    status, reason = ({3: ("completed", "verified"), 1: ("cancelled", "cancel_requested"),
                                       2: ("incomplete", "deadline")})[accepted]
                    if (status == "completed" and len(actions) == 1 and
                            actions[0].get("operation") == "navigate" and
                            any(c.predicate == "current_page" for c in self.task.criteria)):
                        reason = "already_satisfied"
            elif "fail" in assessments:
                status, reason = "failed", "postcondition_failed"
            else:
                status, reason = "incomplete", "verification_unknown"
            if status != "completed":
                try:
                    terminal_gate = self.gate.finish()
                except RuntimeStop:
                    status, reason = "failed", "executor_error"
                else:
                    if terminal_gate == 1:
                        status, reason = "cancelled", "cancel_requested"
                    elif terminal_gate == 2:
                        status, reason = "incomplete", "deadline"
        try:
            self.gate.stop("terminal")
        except RuntimeStop:
            pass
        grace_end = terminal_at + GRACE_SECONDS
        while self.process.is_alive() and time.monotonic() < grace_end:
            try:
                self._record(self.events.get(timeout=min(0.05, max(0.001, grace_end - time.monotonic()))), actions)
            except queue.Empty:
                pass
        cleanup = "closed"
        if self.process.is_alive():
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.process.join(timeout=KILL_SECONDS)
            if self.process.is_alive():
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.join(timeout=0.1)
            cleanup = "forced" if not self.process.is_alive() else "unknown"
        if not self.process.is_alive():
            self.process.join(timeout=0)
        if verification is None:
            verification = self._verify(max(0, grace_end - time.monotonic()))
        while True:
            try:
                self._record(self.events.get_nowait(), actions)
            except queue.Empty:
                break
        # Late evidence can refine an effect, but the accepted terminal status never changes.
        self._reconcile(actions, verification)
        try:
            snapshots = self.gate.snapshot()
        except RuntimeStop:
            snapshots = {"steps": self.gate.steps.value, "model_calls": self.gate.calls.value,
                         "dispatches": self.gate.dispatches.value, "in_flight": self.gate.in_flight.value}
            cleanup = "unknown"
        # A child can commit dispatch and die before the event reaches the parent.
        if snapshots["dispatches"] > len(actions):
            actions.extend({"id": i, "effect": "unknown"} for i in range(len(actions) + 1, snapshots["dispatches"] + 1))
        budget_violation = (snapshots["steps"] > self.task.limits.max_steps or
                            snapshots["model_calls"] > self.task.limits.max_model_calls)
        verification.append({"id": "runtime.budget", "kind": "execution_constraint",
                             "status": "fail" if budget_violation else "pass", "observed_at": time.time(),
                             "evidence_refs": [f"runtime:{self.attempt_id}:budget"]})
        if budget_violation and status != "cancelled":
            status, reason = "failed", "constraint_violated"
        outcome = ("pass" if status == "completed" else "fail" if status == "failed" and
                   reason in ("postcondition_failed", "constraint_violated") else
                   "fail" if budget_violation else "unknown")
        elapsed = round(time.monotonic() - self.started, 3)
        return {"schema_version": "execution-result/0.1", "task_id": self.task.task_id,
                "revision": self.task.revision, "attempt_id": self.attempt_id,
                "executor_id": self.executor_id, "routing": self.routing,
                "attempt_status": status, "stop_reason": reason, "task_outcome": outcome,
                "execution_outcome": "unknown" if snapshots["in_flight"] or any(
                    a["effect"] == "unknown" for a in actions) else
                    "returned" if snapshots["dispatches"] else
                    "error" if status == "failed" else "not_started",
                "verification": verification, "actions": actions, "cleanup": cleanup,
                "trace": self.trace,
                "budget": {"steps": snapshots["steps"], "model_calls": snapshots["model_calls"],
                           "dispatches": snapshots["dispatches"],
                           "remaining_steps": max(0, self.task.limits.max_steps - snapshots["steps"]),
                           "remaining_model_calls": max(0, self.task.limits.max_model_calls - snapshots["model_calls"]),
                           "remaining_wall_seconds": max(0, round(self.gate.deadline - time.monotonic(), 3)),
                           "elapsed_seconds": elapsed, "cost": None, "cost_reason": "not_measured"}}
