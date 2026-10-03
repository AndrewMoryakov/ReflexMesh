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

from reflexmesh.contracts.execution import ExecutionTask, PERMISSIONS

GRACE_SECONDS = 5.0
KILL_SECONDS = 2.0
GATE_ACQUIRE_SECONDS = 0.25
STOP_CODES = {"cancel_requested": "cancelled", "deadline": "incomplete",
              "step_limit": "incomplete", "model_call_limit": "incomplete",
              "effect_unknown": "incomplete",
              "policy_denied": "blocked", "invalid_action": "blocked",
              "adapter_contract_unsupported": "blocked", "no_admissible_action": "blocked",
              "constraint_violated": "failed"}
# Stable bit positions for the shared permission mask; the vocabulary is closed in V0.5.
OPERATION_BITS = {name: 1 << index for index, name in enumerate(sorted(PERMISSIONS))}
CONSTRAINTS = ("permissions", "text_slots", "session", "dispatch_order", "no_uncertain_repeat", "budget")


def permission_mask(operations) -> int:
    mask = 0
    for name in operations:
        mask |= OPERATION_BITS.get(name, 0)
    return mask


class RuntimeStop(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class WorkerResult:
    status: str  # finish | no_confident_action | protocol_error | executor_error | blocked
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
    """Small shared state; no browser/provider call may run under the lock.

    Every accepted dispatch and the first terminal transition take a number from one gate
    sequence, so their local order is comparable after the fact (section 4).
    """

    def __init__(self, ctx, task: ExecutionTask, started: float):
        self.lock = ctx.Lock()
        self.terminal = ctx.Value("i", 0, lock=False)
        self.dispatches = ctx.Value("i", 0, lock=False)
        self.in_flight = ctx.Value("i", 0, lock=False)
        self.steps = ctx.Value("i", 0, lock=False)
        self.calls = ctx.Value("i", 0, lock=False)
        self.seq = ctx.Value("i", 0, lock=False)
        self.terminal_seq = ctx.Value("i", 0, lock=False)
        self.policy_revision = ctx.Value("i", 1, lock=False)
        self.permission_mask = ctx.Value("i", permission_mask(task.permissions), lock=False)
        self.started = started
        self.deadline = started + task.limits.wall_seconds
        self.max_steps = task.limits.max_steps
        self.max_calls = task.limits.max_model_calls

    def _acquire(self):
        if not self.lock.acquire(timeout=GATE_ACQUIRE_SECONDS):
            raise RuntimeStop("gate_unavailable")

    def _terminate(self, code: int) -> None:
        """Caller holds the lock; the first terminal transition is immutable."""
        if not self.terminal.value:
            self.terminal.value = code
            self.seq.value += 1
            self.terminal_seq.value = self.seq.value

    def _check(self):
        if self.terminal.value:
            raise RuntimeStop({1: "cancel_requested", 2: "deadline", 3: "terminal"}[self.terminal.value])
        if time.monotonic() >= self.deadline:
            self._terminate(2)
            raise RuntimeStop("deadline")

    def stop(self, reason: str) -> bool:
        self._acquire()
        try:
            if self.terminal.value or time.monotonic() >= self.deadline:
                self._terminate(2)
                return False
            self._terminate({"cancel_requested": 1, "deadline": 2}.get(reason, 3))
            return True
        finally:
            self.lock.release()

    def finish(self) -> int:
        """Serialize verified completion against cancellation and deadline acceptance."""
        self._acquire()
        try:
            self._terminate(2 if time.monotonic() >= self.deadline else 3)
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

    def revision(self) -> int:
        self._acquire()
        try:
            return self.policy_revision.value
        finally:
            self.lock.release()

    def permitted(self, operation: str) -> bool:
        self._acquire()
        try:
            return bool(self.permission_mask.value & OPERATION_BITS.get(operation, 0))
        finally:
            self.lock.release()

    def revoke(self, operation: str) -> int:
        """Withdraw one permission; returns the gate sequence of the revocation, 0 if refused."""
        self._acquire()
        try:
            if self.terminal.value or operation not in OPERATION_BITS:
                return 0
            self.permission_mask.value &= ~OPERATION_BITS[operation]
            self.policy_revision.value += 1
            self.seq.value += 1
            return self.seq.value
        finally:
            self.lock.release()

    def commit(self, operation: str | None = None, revision: int | None = None) -> tuple[int, int]:
        """The logical dispatch boundary: returns (action_id, gate_seq) or raises RuntimeStop."""
        self._acquire()
        try:
            self._check()
            if not self.steps.value:
                raise RuntimeStop("step_limit")
            if self.in_flight.value:
                raise RuntimeStop("effect_unknown")
            if operation in OPERATION_BITS and not self.permission_mask.value & OPERATION_BITS[operation]:
                raise RuntimeStop("policy_denied")
            if revision is not None and revision != self.policy_revision.value:
                raise RuntimeStop("stale_policy")
            self.dispatches.value += 1
            self.seq.value += 1
            self.in_flight.value = self.dispatches.value
            return self.dispatches.value, self.seq.value
        finally:
            self.lock.release()

    def commit_dispatch(self) -> int:
        return self.commit()[0]

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
            return self._values()
        finally:
            self.lock.release()

    def _values(self):
        return {"steps": self.steps.value, "model_calls": self.calls.value,
                "dispatches": self.dispatches.value, "in_flight": self.in_flight.value,
                "terminal": self.terminal.value, "terminal_seq": self.terminal_seq.value,
                "policy_revision": self.policy_revision.value}


class ControlledEnvironment:
    """Compatible with SystemOneHarness Environment; policy and target checks are injected."""

    def __init__(self, inner, gate: AttemptGate, events,
                 admit: Callable[[str, dict], str | None], *, bootstrap: bool = False,
                 before_commit: Callable[[dict], None] | None = None,
                 after_commit: Callable[[dict], None] | None = None):
        self.inner, self.gate, self.events, self.admit = inner, gate, events, admit
        self.bootstrap = bootstrap
        self.before_commit = before_commit
        self.after_commit = after_commit
        self.last_stop: str | None = None
        self.seen_mutations: set[tuple] = set()
        self.uncertain_mutation = False
        self.observed_revision: int | None = None
        self._session_reported = False
        if hasattr(inner, "permitted"):
            # Candidates are filtered by the live policy, not only the request's permissions.
            inner.permitted = gate.permitted

    def _report_session(self):
        if not self._session_reported and hasattr(self.inner, "session_info"):
            info = self.inner.session_info()
            if info:
                self._session_reported = True
                self.events.put(("session", info))

    def _commit(self, descriptor: dict) -> tuple[int, int]:
        if self.before_commit:
            self.before_commit(descriptor)  # Acceptance fault injection (C13) only.
        operation = descriptor.get("operation")
        return self.gate.commit(operation if operation in OPERATION_BITS else None, self.observed_revision)

    def reset(self, goal):
        if not self.bootstrap:
            self.inner.reset(goal)
            self._report_session()
            return
        self.gate.reserve_step()
        self.events.put(("proposal", {"action": "navigate", "operation": "navigate"}))
        reason = self.admit("navigate", {"bootstrap": True})
        if reason:
            self.last_stop = reason
            raise RuntimeStop(reason)
        descriptor = {"operation": "navigate", "target_id": "bootstrap"}
        self.observed_revision = self.gate.revision()
        try:
            action_id, gate_seq = self._commit(descriptor)
        except RuntimeStop as exc:
            self.last_stop = "policy_denied" if exc.reason == "stale_policy" else exc.reason
            raise RuntimeStop(self.last_stop) from None
        self.events.put(("dispatch", action_id, {**descriptor, "gate_seq": gate_seq}))
        try:
            self.inner.reset(goal)
        except Exception:
            self.events.put(("effect_unknown", action_id))
            raise
        else:
            self.events.put(("driver_return", action_id, True))
        finally:
            self.gate.settle(action_id)
            self._report_session()

    def observe(self):
        try:
            self.gate.reserve_step()
            self.observed_revision = self.gate.revision()
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
        safe = {k: obs.fields[k] for k in ("url", "run_id", "target_ids", "document_id", "content_gaps")
                if hasattr(obs, "fields") and k in obs.fields}
        if safe:
            safe["captured_at"] = time.time()
            self.events.put(("observation", safe))

    def _reject(self, reason: str):
        self.last_stop = reason
        self.events.put(("rejected", reason))
        raise RuntimeStop(reason)

    def execute(self, action, params):
        descriptor = self.inner.describe(action, params) if hasattr(self.inner, "describe") else {"operation": action}
        # Never copy free-form provider parameters into the public trace; slot refs are names.
        self.events.put(("proposal", {"action": action, **{k: descriptor[k] for k in
                                                          ("operation", "target_id", "slot_ref")
                                                          if k in descriptor}}))
        reason = self.admit(action, params)
        if reason == "stale_target":
            self.events.put(("reobserve", "stale_target"))
            return SkippedAction()
        if reason:
            self._reject(reason)
        operation = descriptor.get("operation")
        if operation in OPERATION_BITS and not self.gate.permitted(operation):
            self._reject("policy_denied")
        operation_key = (operation, descriptor.get("target_id"), descriptor.get("slot_ref"))
        mutating = operation != "navigate"
        if mutating and self.uncertain_mutation:
            self._reject("effect_unknown")
        if mutating and operation_key in self.seen_mutations:
            self._reject("invalid_action")
        try:
            action_id, gate_seq = self._commit(descriptor)
        except RuntimeStop as exc:
            if exc.reason == "stale_policy":
                # The policy changed after this candidate was observed but still permits it.
                self.events.put(("reobserve", "stale_policy"))
                return SkippedAction()
            if exc.reason == "policy_denied":
                self._reject("policy_denied")
            self.last_stop = exc.reason
            raise
        if mutating:
            self.seen_mutations.add(operation_key)
        self.events.put(("dispatch", action_id, {**descriptor, "gate_seq": gate_seq}))
        try:
            if self.after_commit:
                self.after_commit(descriptor)  # Acceptance fault injection (C06/C07) only.
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
        rows = (verifier(task, timeout, observation) if len(parameters) >= 3
                else verifier(task, timeout))
        effects = None
        if callable(getattr(verifier, "effects", None)):
            try:
                effects = verifier.effects(task, timeout)
            except Exception:
                effects = None
        result_queue.put({"criteria": rows, "effects": effects})
    except BaseException:
        result_queue.put(None)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class AttemptSupervisor:
    """Supervisor process retains ownership after a worker is stopped or terminated."""

    def __init__(self, task: ExecutionTask, strategy, verifier=None, *, verifier_factory=None,
                 attempt_id: str | None = None):
        self.task, self.strategy, self.verifier = task, strategy, verifier
        self.verifier_factory = verifier_factory
        self.context = mp.get_context("fork")
        self.started = time.monotonic()
        self.gate = AttemptGate(self.context, task, self.started)
        self.events = self.context.Queue()
        self.process = self.context.Process(target=_work, args=(strategy, self.gate, self.events), daemon=False)
        self.attempt_id = attempt_id or uuid.uuid4().hex
        self._cancel_requested = False
        self.observation = None
        self.trace = []
        self.routing = None
        self.executor_id = None
        self.sessions: list[dict] = []
        self.revocations: list[dict] = []
        self.effects = None
        self.decisions: list[dict] = []
        self._last_proposal_seq = None

    def cancel(self):
        try:
            accepted = self.gate.stop("cancel_requested")
        except RuntimeStop:
            return False
        self._cancel_requested = accepted or self.gate.terminal.value == 1
        return accepted

    def revoke(self, operation: str) -> bool:
        """Withdraw a permission for the rest of the attempt (policy revision, C13)."""
        try:
            seq = self.gate.revoke(operation)
        except RuntimeStop:
            return False
        if seq:
            self.revocations.append({"operation": operation, "gate_seq": seq})
        return bool(seq)

    def _verify(self, remaining: float) -> list[dict]:
        unknown = [{"id": c.id, "kind": "postcondition", "status": "unknown",
                    "observed_at": None, "evidence_refs": []} for c in self.task.criteria]
        value = self._run_verifier(remaining)
        rows = value.get("criteria") if type(value) is dict else None
        if type(value) is dict and type(value.get("effects")) is dict:
            self.effects = value["effects"]
        if (type(rows) is not list or len(rows) != len(self.task.criteria) or
                any(type(c) is not dict or c.get("status") not in ("pass", "fail", "unknown") or
                    c.get("id") != expected.id for c, expected in zip(rows, self.task.criteria))):
            return unknown
        return [{**row, "kind": "postcondition", "observed_at": row.get("observed_at")}
                for row in rows]

    def _run_verifier(self, remaining: float):
        if not self.verifier or remaining <= 0:
            return None
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
        return value

    def _record(self, event, actions):
        if event[0] in ("dispatch", "driver_return", "effect_unknown", "rejected", "reobserve",
                        "decision_request", "step", "stopped", "done", "observation", "proposal",
                        "session", "revoked", "decision"):
            self.trace.append({"seq": len(self.trace) + 1, "t": round(time.monotonic() - self.started, 3),
                               "kind": event[0], "data": list(event[1:])})
        seq = len(self.trace)
        if event[0] == "baseline":
            if self.verifier_factory and self.verifier is None:
                self.verifier = self.verifier_factory(self.task, event[1])
        elif event[0] == "routing":
            self.routing = event[1]
        elif event[0] == "executor_selected":
            self.executor_id = event[1]
        elif event[0] == "session":
            if type(event[1]) is dict:
                self.sessions.append(event[1])
        elif event[0] == "decision":
            if type(event[1]) is dict:
                self.decisions.append(event[1])
        elif event[0] == "revoked":
            self.revocations.append({"operation": event[1], "gate_seq": event[2]})
        elif event[0] == "proposal":
            self._last_proposal_seq = seq
        elif event[0] == "dispatch":
            actions.append({"id": event[1], **event[2], "effect": "unknown",
                            "proposal_seq": self._last_proposal_seq,
                            "dispatch_seq": seq, "return_seq": None, "evidence_refs": []})
        elif event[0] == "driver_return":
            for row in actions:
                if row["id"] == event[1]:
                    row["driver_returned"] = True
                    row["driver_ok"] = bool(event[2]) if len(event) > 2 else True
                    row["return_seq"] = seq
        elif event[0] == "effect_unknown":
            for row in actions:
                if row["id"] == event[1]:
                    row["driver_error"] = True
        elif event[0] == "observation":
            self.observation = event[1]
            for row in actions:
                if (row.get("operation") == "navigate" and row.get("driver_returned") and
                        row.get("effect") == "unknown" and
                        event[1].get("url") == row.get("destination", self.task.origin + self.task.start_path)):
                    row["effect"] = "applied"
                    row["evidence_refs"] = [f"browser:{self.attempt_id}:{seq}"]

    def _reconcile(self, actions, verification, *, browser_stopped: bool = False):
        """Update effects from authoritative evidence; never changes the terminal status."""
        covered = {"form_submitted_once": {"submit_form", "type_text"},
                   "settings_saved": {"save_settings", "toggle_setting"},
                   "export_completed_once": {"start_export"},
                   "support_request_sent_once": {"submit_form", "type_text"}}
        validated = set().union(*(covered.get(c.predicate, set())
                                  for c, row in zip(self.task.criteria, verification) if row["status"] == "pass"))
        refs = list(dict.fromkeys(ref for row in verification if row["status"] == "pass"
                                  for ref in row.get("evidence_refs", [])))
        for row in actions:
            if row.get("operation") in validated:
                row["effect"] = "applied"
                row["evidence_refs"] = refs
        effects = self.effects if type(self.effects) is dict else {}
        ref = effects.get("ref")
        targets = effects.get("targets") if type(effects.get("targets")) is dict else {}
        for target_id, info in targets.items():
            # Authoritative per-target record of mutating requests since the baseline.
            rows = [row for row in actions if row.get("target_id") == target_id and row.get("operation") != "navigate"]
            if type(info) is not dict or not rows or type(info.get("requests")) is not int:
                continue
            pending = [row for row in rows if row["effect"] == "unknown"]
            if not pending:
                continue
            requests, running = info["requests"], bool(info.get("running"))
            if requests == len(rows) and not running:
                for row in pending:
                    row["effect"] = "applied"
                    row["evidence_refs"] = [ref] if ref else []
            elif requests == 0 and not running and browser_stopped:
                # The browser is gone and the fixture recorded no request: nothing can still arrive.
                for row in pending:
                    row["effect"] = "not_applied"
                    row["evidence_refs"] = [ref] if ref else []
        return validated

    def _constraints(self, actions, snap) -> list[dict]:
        """Runtime-installed execution constraints (section 8); missing evidence is unknown."""
        now = time.time()
        complete = (snap["dispatches"] == len(actions) and
                    all("dispatch_seq" in row and "operation" in row for row in actions))
        vocabulary = all(row.get("operation") in OPERATION_BITS for row in actions)
        status = {}

        # Permission enforcement: every committed operation was granted and not yet revoked.
        if any(row.get("operation") in OPERATION_BITS and row["operation"] not in self.task.permissions
               for row in actions):
            status["permissions"] = "fail"
        elif any(rev["operation"] == row.get("operation") and row.get("gate_seq", 0) > rev["gate_seq"]
                 for rev in self.revocations for row in actions):
            status["permissions"] = "fail"
        else:
            status["permissions"] = "pass" if complete and vocabulary else "unknown"

        # Immutable slot use: typing only ever references an exact declared slot version.
        refs = {slot.reference for slot in self.task.slots}
        typed = [row for row in actions if row.get("operation") == "type_text"]
        if any("slot_ref" in row and row["slot_ref"] not in refs for row in typed):
            status["text_slots"] = "fail"
        elif not complete or any("slot_ref" not in row for row in typed):
            status["text_slots"] = "unknown"
        else:
            status["text_slots"] = "pass"

        # Single session ownership: one fresh, launched (not attached) browser session.
        ids = {session.get("session_id") for session in self.sessions}
        if len(ids) > 1 or any(session.get("attached") is not False or session.get("fresh_profile") is not True
                               for session in self.sessions):
            status["session"] = "fail"
        elif not snap["dispatches"] and not self.sessions:
            status["session"] = "pass"
        else:
            status["session"] = "pass" if len(ids) == 1 and None not in ids else "unknown"

        # Dispatch ordering: proposals precede commits, commits are sequential, none after terminal.
        terminal_seq = snap.get("terminal_seq") or 0
        ordered = sorted(actions, key=lambda row: row["id"])
        gate_seqs = [row.get("gate_seq") for row in ordered]
        if terminal_seq and any(type(value) is int and value > terminal_seq for value in gate_seqs):
            status["dispatch_order"] = "fail"
        elif not complete or any(type(value) is not int for value in gate_seqs):
            status["dispatch_order"] = "unknown"
        elif ([row["id"] for row in ordered] != list(range(1, len(ordered) + 1)) or
              gate_seqs != sorted(set(gate_seqs)) or
              any(row.get("proposal_seq") is None or row["proposal_seq"] >= row["dispatch_seq"] or
                  (row.get("return_seq") is not None and row["return_seq"] <= row["dispatch_seq"])
                  for row in ordered)):
            status["dispatch_order"] = "fail"
        else:
            status["dispatch_order"] = "pass"

        # No uncertain repeats: no second commit of one mutation, none after an uncertain mutation.
        seen, uncertain, violated = set(), False, False
        for row in ordered:
            if row.get("operation") == "navigate":
                continue
            key = (row.get("operation"), row.get("target_id"), row.get("slot_ref"))
            if uncertain or key in seen:
                violated = True
            seen.add(key)
            if row.get("driver_error") or row.get("driver_ok") is False:
                uncertain = True
        status["no_uncertain_repeat"] = "fail" if violated else ("pass" if complete else "unknown")

        status["budget"] = ("fail" if snap["steps"] > self.task.limits.max_steps or
                            snap["model_calls"] > self.task.limits.max_model_calls else "pass")
        return [{"id": f"runtime.{name}", "kind": "execution_constraint", "status": status[name],
                 "observed_at": now, "evidence_refs": [f"runtime:{self.attempt_id}:{name}"]}
                for name in CONSTRAINTS]

    def _snapshot(self):
        try:
            return self.gate.snapshot()
        except RuntimeStop:
            # A worker that died holding the gate cannot change these values any more.
            return self.gate._values()

    def _dispatch_violation(self, row) -> bool:
        operation = row.get("operation")
        if operation in OPERATION_BITS and operation not in self.task.permissions:
            return True
        return (operation == "type_text" and "slot_ref" in row and
                row["slot_ref"] not in {slot.reference for slot in self.task.slots})

    def _accept(self, status, reason):
        """Accept a terminal transition through the gate; cancellation/deadline may win."""
        try:
            terminal_gate = self.gate.finish()
        except RuntimeStop:
            return "failed", "executor_error"
        if terminal_gate == 1:
            return "cancelled", "cancel_requested"
        if terminal_gate == 2:
            return "incomplete", "deadline"
        return status, reason

    def run(self) -> dict:
        status = reason = None
        actions: list[dict] = []
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
                self._record(event, actions)
                if event[0] == "dispatch" and self._dispatch_violation(actions[-1]):
                    status, reason = "failed", "constraint_violated"
                elif event[0] == "rejected":
                    status, reason = STOP_CODES.get(event[1], "blocked"), event[1]
                elif event[0] == "stopped":
                    reason = event[1]
                    status = STOP_CODES.get(reason, "failed")
                    if status == "failed":
                        reason = "executor_error"
                elif event[0] == "done":
                    kind = event[1]
                    if kind in ("finish", "no_confident_action"):
                        status, reason = "verifying", kind
                    elif kind == "blocked":
                        status, reason = "blocked", event[2]
                    else:
                        status, reason = "failed", ("protocol_error" if kind == "protocol_error" else "executor_error")
        terminal_at = time.monotonic()
        verification = None
        if status != "verifying":
            if status not in ("cancelled", "failed") and any(
                    row["status"] == "fail" for row in self._constraints(actions, self._snapshot())):
                status, reason = "failed", "constraint_violated"
            status, reason = self._accept(status, reason)
        else:
            verification = self._verify(max(0, self.gate.deadline - terminal_at))
            assessments = [c["status"] for c in verification]
            validated = self._reconcile(actions, verification)
            snap = self._snapshot()
            constraints = [row["status"] for row in self._constraints(actions, snap)]
            unknown_mutations = any(row.get("operation") not in validated and
                                    row.get("operation") != "navigate" and row["effect"] != "applied"
                                    for row in actions)
            if "fail" in constraints:
                status, reason = "failed", "constraint_violated"
            elif (assessments and all(v == "pass" for v in assessments) and
                    all(v == "pass" for v in constraints) and
                    not snap["in_flight"] and len(actions) == snap["dispatches"] and
                    not unknown_mutations and all(row["effect"] != "unknown" for row in actions)):
                status, reason = "completed", "verified"
                if (len(actions) == 1 and actions[0].get("operation") == "navigate" and
                        any(c.predicate == "current_page" for c in self.task.criteria)):
                    reason = "already_satisfied"
            elif "fail" in assessments:
                status, reason = "failed", "postcondition_failed"
            else:
                status, reason = "incomplete", "verification_unknown"
            status, reason = self._accept(status, reason)
        terminal_at = time.monotonic()
        snap = self._snapshot()
        self.trace.append({"seq": len(self.trace) + 1, "t": round(terminal_at - self.started, 3), "kind": "terminal",
                           "data": [status, reason, snap.get("terminal_seq")]})
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
        cleanup_started = time.monotonic()
        cleanup = "closed"
        pgid = self.process.pid
        if self.process.is_alive() or _group_alive(pgid):
            # The worker or a browser descendant outlived the grace period.
            for sig, wait in ((signal.SIGTERM, KILL_SECONDS), (signal.SIGKILL, 0.25)):
                try:
                    os.killpg(pgid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
                if self.process.is_alive():
                    # The worker may not have become a group leader yet.
                    try:
                        os.kill(self.process.pid, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
                limit = time.monotonic() + wait
                while time.monotonic() < limit and (self.process.is_alive() or _group_alive(pgid)):
                    self.process.join(timeout=0.02)
                if not (self.process.is_alive() or _group_alive(pgid)):
                    break
            cleanup = "forced" if not (self.process.is_alive() or _group_alive(pgid)) else "unknown"
        if not self.process.is_alive():
            self.process.join(timeout=0)
        cleanup_seconds = round(time.monotonic() - cleanup_started, 3)
        while True:
            try:
                self._record(self.events.get_nowait(), actions)
            except queue.Empty:
                break
        snap = self._snapshot()
        # A child can commit dispatch and die before the event reaches the parent.
        if snap["dispatches"] > len(actions):
            actions.extend({"id": i, "effect": "unknown"} for i in range(len(actions) + 1, snap["dispatches"] + 1))
        # Late, read-only evidence: bounded by the remaining collection allowance.
        collection_end = terminal_at + GRACE_SECONDS + KILL_SECONDS
        if verification is None or any(row["effect"] == "unknown" for row in actions):
            fresh = self._verify(max(0.0, min(2.0, collection_end - time.monotonic())))
            if verification is None or all(row["status"] != "unknown" for row in fresh):
                verification = fresh
        # Late evidence can refine an effect, but the accepted terminal status never changes.
        self._reconcile(actions, verification, browser_stopped=cleanup in ("closed", "forced"))
        constraints = self._constraints(actions, snap)
        verification = [row for row in verification if row.get("kind") == "postcondition"] + constraints
        violated = any(row["status"] == "fail" for row in constraints)
        outcome = ("pass" if status == "completed" and not violated else
                   "fail" if violated or (status == "failed" and
                                          reason in ("postcondition_failed", "constraint_violated")) else
                   "unknown")
        elapsed = round(time.monotonic() - self.started, 3)
        return {"schema_version": "execution-result/0.2", "task_id": self.task.task_id,
                "revision": self.task.revision, "attempt_id": self.attempt_id,
                "executor_id": self.executor_id, "routing": self.routing,
                "attempt_status": status, "stop_reason": reason, "task_outcome": outcome,
                # An in-flight action stays unknown unless authoritative evidence resolved its effect.
                "execution_outcome": "unknown" if any(a["effect"] == "unknown" for a in actions) or (
                    snap["in_flight"] and snap["in_flight"] not in {a["id"] for a in actions}) else
                    "returned" if snap["dispatches"] else
                    "error" if status == "failed" else "not_started",
                "chain": {"root_attempt_id": self.attempt_id, "parent_attempt_id": None, "sequence": 1},
                "handoff": None,
                "verification": verification, "actions": actions, "cleanup": cleanup,
                "trace": self.trace,
                "policy": {"revision": snap.get("policy_revision"), "revocations": self.revocations},
                "timing": {"terminal_after_seconds": round(terminal_at - self.started, 3),
                           "deadline_delay_seconds": (round(terminal_at - self.gate.deadline, 3)
                                                      if reason == "deadline" else None),
                           "cleanup_seconds": cleanup_seconds,
                           "exit_after_terminal_seconds": round(time.monotonic() - terminal_at, 3)},
                "budget": {"steps": snap["steps"], "model_calls": snap["model_calls"],
                           "dispatches": snap["dispatches"],
                           "remaining_steps": max(0, self.task.limits.max_steps - snap["steps"]),
                           "remaining_model_calls": max(0, self.task.limits.max_model_calls - snap["model_calls"]),
                           "remaining_wall_seconds": max(0, round(self.gate.deadline - time.monotonic(), 3)),
                           "elapsed_seconds": elapsed, **self._usage(snap)}}

    def _usage(self, snap) -> dict:
        costs = [row.get("cost") for row in self.decisions]
        tokens = {key: sum(row.get(key) or 0 for row in self.decisions) for key in ("input_tokens", "output_tokens")}
        models = sorted({row.get("served_model") for row in self.decisions if row.get("served_model")})
        if not snap["model_calls"]:
            cost, reason = 0.0, "no_model_calls"
        elif len(costs) == snap["model_calls"] and all(type(c) in (int, float) for c in costs):
            cost, reason = round(sum(costs), 8), "provider_reported"
        else:
            cost, reason = None, "not_reported_for_every_model_call"
        return {"model_usage": {"micro_decisions": len(self.decisions), **tokens, "served_models": models},
                "cost": cost, "cost_reason": reason}
