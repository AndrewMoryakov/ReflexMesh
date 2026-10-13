"""One controlled attempt of a `browser-task/0.1` (V0.6, M1-M2).

The loop is SystemOneHarness's: observe, ask a decision provider, gate, act. This module adds
what ReflexMesh owns around it: every observation spends a step, every model request spends a
call, every browser command passes the shared dispatch gate, and a redacted journal records
the chain options -> decision -> admission -> dispatch -> driver -> observed effect.

What this build does not do, stated here so a green run is not misread:
  * no process isolation (the loop runs in a thread; a watchdog kills the browser on a hang),
  * criteria are judged on the page (section 11), but `completed` also needs every runtime constraint
    and every action effect settled, which this build cannot yet establish on an arbitrary site,
  * no handoff continuation (M3), only the package that explains the stop.
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import multiprocessing as mp
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from reflexmesh.adapters.system_one.browser_adapter import (
    CAPABILITIES, GenericBrowserAdapter, browser_action_space, chrome_version)
from reflexmesh.adapters.system_one.decision_providers import ProviderUnavailable, ValidatingProvider
from reflexmesh.adapters.system_one.harness import CountingProvider
from reflexmesh.contracts.browser import BrowserTask, canonical_origin
from reflexmesh.runtime.handoff import standalone_chain
from reflexmesh.runtime.runner import (
    AttemptGate, KILL_SECONDS, GRACE_SECONDS, RuntimeStop, SkippedAction, STOP_CODES, WorkerResult)
from reflexmesh.text.slots import SlotRegistry
from reflexmesh.tracing.slot_evidence import SlotEvidence
from reflexmesh.verification.constraints import ConstraintAssessments, assess_constraints
from reflexmesh.verification.page_verifier import PREDICATES as PAGE_PREDICATES, judge, normalized

RESULT_SCHEMA = "browser-result/0.1"
SOH_PIN = "ab8e8f08b4a6268c0633a474423d556599ee06a4"
BROWSER_USE_PIN = "5c892e013a73e6622e6f50336e1eb0aa2c4405f2"
STALE_LIMIT = 3
NO_PROGRESS_LIMIT = 3  # cycles in a row whose observation shows no new essential state (V0.6 section 8)
HANDOFF_REASONS = {"no_confident_action": "no_confident_action", "escalation_requested": "escalation_requested",
                   "repeated_action": "no_progress", "stale_loop": "stale_loop"}
MIN_SCRUBBED_NORMAL = 4


class AttemptTask:
    """The accepted task plus the attempt identity that the gate, slot ledger and constraint rows bind to."""

    def __init__(self, task: BrowserTask, attempt_id: str, limits=None):
        self._task = task
        self.run_id = attempt_id
        self._limits = limits   # a continued attempt sees what is left of the chain's budget

    @property
    def limits(self):
        return self._limits if self._limits is not None else self._task.limits

    def __getattr__(self, name):
        return getattr(self._task, name)


class Journal:
    """Ordered, redacted record of one attempt; also the event sink the controlled environment writes to.

    Slot values never appear: secrets are always replaced, normal values of four characters or more
    too (a shorter value cannot be told apart from ordinary page text). The journal keeps references,
    structural descriptors and counts, never page text or typed content.
    """

    def __init__(self, task: BrowserTask, attempt_id: str, path: Path | None, producer: str):
        self.attempt_id, self.producer = attempt_id, producer
        values = [(s.value, s.reference) for s in task.slots
                  if s.value and (s.sensitivity == "secret" or len(s.value) >= MIN_SCRUBBED_NORMAL)]
        self._values = sorted(values, key=lambda item: -len(item[0]))
        self.records: list[dict] = []
        self.actions: list[dict] = []
        self.outcomes: dict[int, str] = {}
        self.current_observation: int | None = None
        self.last_observation: dict | None = None
        self._proposal_seq: int | None = None
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._file = open(path, "w", encoding="utf-8") if path else None

    def scrub(self, value):
        if type(value) is str:
            for text, reference in self._values:
                value = value.replace(text, f"[slot:{reference}]")
            return value
        if type(value) in (list, tuple):
            return [self.scrub(item) for item in value]
        if type(value) is dict:
            return {key: self.scrub(item) for key, item in value.items()}
        return value

    def emit(self, kind: str, **data) -> int:
        with self._lock:
            seq = len(self.records) + 1
            record = {"seq": seq, "t": round(time.monotonic() - self._t0, 3), "kind": kind, **self.scrub(data)}
            self.records.append(record)
            if self._file is not None:
                self._file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                self._file.flush()
            return seq

    def observed(self, report: dict, effect: dict | None) -> None:
        self.current_observation = report["observation_id"]
        parts = urlsplit(report["url"])
        where = f"{parts.scheme}://{parts.netloc}{parts.path}"
        seq = self.emit("observed", observation_id=report["observation_id"], page=where,
                        coverage=report["coverage"], text_chars=report["text_chars"],
                        options=report["options"], page_signature=report["signature"])
        self.last_observation = {"observation_id": report["observation_id"], "page": where, "seq": seq}
        self.emit("options_built", observation_id=report["observation_id"], options=report["option_list"])
        if effect is not None:
            self.emit("effect_assessed", **effect)
            for row in self.actions:
                if row["id"] == effect.get("action_id"):
                    row["ui_effect"] = {k: v for k, v in effect.items() if k not in ("action_id", "operation")}
        for row in self.actions:  # a navigation is settled by the next observation of its destination
            if (row["operation"] == "navigate" and row["effect"] == "unknown" and row.get("driver_outcome") == "returned"
                    and (row.get("return_seq") or 10 ** 9) < seq
                    and canonical_origin(report["url"]) == canonical_origin(row.get("destination") or "")
                    and urlsplit(report["url"]).path == urlsplit(row.get("destination") or "").path):
                row["effect"] = "applied"
                row["evidence_refs"] = [f"browser:{self.attempt_id}:{seq}"]

    def put(self, event: tuple) -> None:
        kind = event[0]
        if kind == "proposal":
            self._proposal_seq = self.emit("proposal", **event[1])
        elif kind == "dispatch":
            action_id, desc = event[1], self.scrub(dict(event[2]))
            seq = self.emit("dispatch_committed", action_id=action_id, **desc)
            self.actions.append({
                "id": action_id, "operation": desc.get("operation"), "risk": desc.get("risk"),
                "option_ref": desc.get("option_ref"), "target_id": desc.get("target_id"),
                "mutation_key": desc.get("mutation_key"), "request": desc.get("request"),
                "target": desc.get("target"), "slot_ref": desc.get("slot_ref"),
                "observation_id": desc.get("observation_id"), "producer": desc.get("producer") or self.producer,
                "bootstrap": desc.get("bootstrap", False), "destination": desc.get("destination"),
                "policy_revision": desc.get("policy_revision"), "gate_seq": desc.get("gate_seq"),
                "proposal_seq": self._proposal_seq, "dispatch_seq": seq, "return_seq": None,
                "driver_outcome": None, "effect": "unknown", "evidence_refs": []})
        elif kind in ("driver_return", "effect_unknown"):
            action_id = event[1]
            ok = kind == "driver_return" and len(event) > 2 and event[2] is True
            seq = self.emit("driver_returned", action_id=action_id, ok=ok, error=kind == "effect_unknown")
            self.outcomes[action_id] = "ok" if ok else ("error" if kind == "effect_unknown" else "not_ok")
            for row in self.actions:
                if row["id"] == action_id:
                    row["driver_outcome"] = "error" if kind == "effect_unknown" else "returned"
                    row["driver_ok"] = ok
                    row["return_seq"] = seq
        elif kind == "rejected":
            self.emit("admission_rejected", reason=event[1])
        elif kind == "reobserve":
            self.emit("reobserve", reason=event[1])
        # decision_request / step: the provider wrapper writes the richer decision events itself.

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


class CriteriaVerifier:
    """Task criteria judged by the runtime on fresh observations (V0.6 sections 8 and 11), never by the
    model's word. Every cycle looks at the page (no navigation); a `revisit_match` passes only after the
    verifier itself opened the document again through the dispatch gate."""

    def __init__(self, task: BrowserTask, attempt_id: str, journal: Journal, checks: dict | None = None):
        self.task, self.attempt_id, self.journal = task, attempt_id, journal
        self.checks = checks or {}
        self.rows = {c.id: {"id": c.id, "kind": "postcondition", "predicate": c.predicate, "status": "unknown",
                            "reason": "not_assessed", "scope": None, "observed_at": None, "evidence_refs": []}
                     for c in task.criteria}
        self.outputs: dict[str, dict] = {}
        self.ready: dict[str, bool] = {}   # revisit_match whose page check passed and awaits the reopen

    def _slot(self, ref: str):
        return next((s for s in self.task.slots if s.reference == ref), None)

    def _registered(self, criterion, evidence: dict | None = None) -> dict:
        """A registered check judges from its own source; `evidence` is passed when the caller already read it."""
        check = self.checks.get(criterion.args.get("check_id"))
        if check is None:
            return {"status": "unknown", "reason": "no_registered_check", "facts": {}}
        expected = [self._slot(ref) for ref in criterion.args["expected_slot_refs"]]
        if any(slot is None or slot.sensitivity != "normal" for slot in expected):
            return {"status": "unknown", "reason": "secret_slot_not_compared", "facts": {}}
        if evidence is None:
            try:
                evidence = check.evidence(timeout=1.0)
            except Exception:
                evidence = None
        return check.judge(criterion.args, [slot.value for slot in expected], evidence)

    def settle_registered(self, evidence_by_check: dict) -> None:
        """The final word of each registered check, read after the browser stopped (V0.6 section 11)."""
        for criterion in self.task.criteria:
            if criterion.predicate == "registered_check":
                verdict = self._registered(criterion, evidence_by_check.get(criterion.args["check_id"]))
                self._record(criterion, verdict, "registered", always=True)

    def _judge(self, criterion, adapter) -> dict:
        if criterion.predicate == "registered_check":
            return self._registered(criterion)
        if criterion.predicate not in PAGE_PREDICATES:
            return {"status": "unknown", "reason": "no_registered_check", "facts": {}}
        anchor = self._slot(criterion.args["anchor_slot_ref"])
        expected = self._slot(criterion.args["expected_slot_ref"]) if "expected_slot_ref" in criterion.args else None
        if anchor is None or anchor.sensitivity != "normal" or (expected is not None and expected.sensitivity != "normal"):
            return {"status": "unknown", "reason": "secret_slot_not_compared", "facts": {}}
        try:
            probe = adapter.inspect(normalized(anchor.value), normalized(criterion.args["field_name"]))
        except Exception:
            probe = None
        return judge(criterion.predicate, probe, expected.value if expected is not None else None)

    def _record(self, criterion, verdict: dict, stage: str, *, always: bool = False) -> None:
        row = self.rows[criterion.id]
        status, reason = verdict["status"], verdict["reason"]
        if criterion.predicate == "revisit_match" and stage == "page":
            # The page shows the value; the criterion still needs the reopen.
            self.ready[criterion.id] = status == "pass"
            if status == "pass":
                status, reason = "unknown", "not_reopened"
        if not always and (row["status"], row["reason"]) == (status, reason):
            return
        seq = self.journal.emit("criterion_assessed", id=criterion.id, predicate=criterion.predicate, status=status,
                                reason=reason, stage=stage, **verdict["facts"])
        scope = {"reopened": "ui_after_reopen", "registered": "registered_source"}.get(
            stage, "registered_source_while_running" if criterion.predicate == "registered_check" else "ui_observation")
        row.update(status=status, reason=reason, scope=scope,
                   observed_at=time.time(), evidence_refs=[f"browser:{self.attempt_id}:{seq}"])
        if criterion.predicate == "scoped_extract":
            if status == "pass":
                self.outputs[criterion.id] = {"criterion_id": criterion.id, "key": criterion.args["output_key"],
                                              "value": verdict["value"], "evidence_refs": list(row["evidence_refs"])}
            else:
                self.outputs.pop(criterion.id, None)

    def page_check(self, adapter) -> bool:
        """Judge every criterion on the page as it is now; True when each one holds here."""
        held = True
        for criterion in self.task.criteria:
            verdict = self._judge(criterion, adapter)
            self._record(criterion, verdict, "page")
            held = held and verdict["status"] == "pass"
        return held and bool(self.task.criteria)

    def final(self, env: "BrowserControlledEnvironment") -> None:
        """The verification that may complete the attempt: a fresh look, then one reopen for revisit_match."""
        adapter = env.inner
        for criterion in self.task.criteria:
            self._record(criterion, self._judge(criterion, adapter), "page", always=True)
        waiting = [c for c in self.task.criteria if c.predicate == "revisit_match" and self.ready.get(c.id)]
        if not waiting:
            return
        before = adapter.document_id()
        reopened = env.reopen()
        for criterion in waiting:
            if not reopened or adapter.document_id() in (None, before):
                self._record(criterion, {"status": "unknown", "reason": "not_reopened", "facts": {}}, "reopened",
                             always=True)
            else:
                self._record(criterion, self._judge(criterion, adapter), "reopened", always=True)

    def result_rows(self) -> list[dict]:
        return [dict(row, evidence_refs=list(row["evidence_refs"])) for row in self.rows.values()]


class BrowserControlledEnvironment:
    """SystemOneHarness `Environment` whose every command passes admission and the dispatch gate."""

    def __init__(self, inner: GenericBrowserAdapter, gate: AttemptGate, journal: Journal, task: BrowserTask):
        self.inner, self.gate, self.journal, self.task = inner, gate, journal, task
        self.last_stop: str | None = None
        self.uncertain = False
        self.observed_revision: int | None = None
        self._stale_streak = 0
        self._last_state: str | None = None
        self._unchanged = 0
        self._done_in_state: set[str] = set()
        self._mutations: set[str] = set()
        self.verifier: CriteriaVerifier | None = None
        self.continued = False   # an attempt that continues a chain: same session, no first navigation

    def carry(self) -> dict:
        """What the repeat guard remembers; it belongs to the chain, not to one attempt (V0.6 section 9)."""
        return {"mutations": set(self._mutations), "done_in_state": set(self._done_in_state)}

    def restore(self, carry: dict | None) -> None:
        if carry:
            self._mutations |= carry["mutations"]
            self._done_in_state |= carry["done_in_state"]

    def _stop(self, reason: str) -> RuntimeStop:
        self.last_stop = reason
        return RuntimeStop(reason)

    def reset(self, goal):
        """The first navigation is a dispatch like any other: permission, step, gate, journal. A continued
        attempt has none: it looks at the page the session is on (section 10)."""
        if self.continued:
            return
        self.gate.reserve_step()
        self.journal.put(("proposal", {"action": "navigate"}))
        if "navigate" not in self.task.permissions or canonical_origin(self.task.start_url) not in self.task.allowed_origins:
            self.journal.put(("rejected", "policy_denied"))
            raise self._stop("policy_denied")
        self.observed_revision = self.gate.revision()
        try:
            action_id, gate_seq = self.gate.commit_dispatch_seq("nontext", operation="navigate",
                                                                revision=self.observed_revision)
            if self.gate.slot_sink is not None:
                # Every committed dispatch is accounted for in the slot ledger, the first one included.
                self.gate.slot_sink.commit(action_id, "nontext", None)
        except RuntimeStop as exc:
            raise self._stop("policy_denied" if exc.reason == "stale_policy" else exc.reason) from None
        parts = urlsplit(self.task.start_url)
        self.journal.put(("dispatch", action_id, {
            "operation": "navigate", "risk": "write", "bootstrap": True, "gate_seq": gate_seq,
            "observation_id": 0, "policy_revision": self.observed_revision,
            "target_id": "start_url", "destination": f"{parts.scheme}://{parts.netloc}{parts.path or '/'}"}))
        try:
            self.inner.reset(goal)
        except Exception:
            self.journal.put(("effect_unknown", action_id))
            raise
        else:
            self.journal.put(("driver_return", action_id, True))
        finally:
            self.gate.settle(action_id)

    def observe(self):
        try:
            self.gate.reserve_step()
            self.observed_revision = self.gate.revision()
            obs = self.inner.observe()
        except RuntimeStop as exc:
            self.last_stop = exc.reason
            raise
        effect, self.inner.effect_report = self.inner.effect_report, None
        self.journal.observed(self.inner.report, effect)
        if self.verifier is not None and self.verifier.page_check(self.inner):
            # Every criterion holds on this page: the runtime verifies now, whatever the model would say next.
            raise self._stop("criteria_met")
        state = self.inner.report.get("signature")
        self._unchanged = self._unchanged + 1 if state is not None and state == self._last_state else 0
        self._last_state = state
        if self._unchanged >= NO_PROGRESS_LIMIT:
            # Three cycles and the page is where it was: no new state, and no effect or criterion this build
            # can see move. Stopping here is the rule, not a judgement on the model.
            raise self._stop("no_progress")
        return obs

    def reopen(self) -> bool:
        """The verifier opens the observed document again: a navigation like any other (permission, origin,
        step, gate, journal), produced by the verifier, never a resubmitted form."""
        url = str(self.inner.report.get("url") or "")
        if "navigate" not in self.task.permissions or canonical_origin(url) not in self.task.allowed_origins:
            return False
        try:
            self.gate.reserve_step()
            self.journal.put(("proposal", {"action": "navigate", "producer": "verifier"}))
            self.observed_revision = self.gate.revision()
            action_id, gate_seq = self.gate.commit_dispatch_seq("nontext", operation="navigate",
                                                                revision=self.observed_revision)
            if self.gate.slot_sink is not None:
                self.gate.slot_sink.commit(action_id, "nontext", None)
        except RuntimeStop:
            return False
        parts = urlsplit(url)
        self.journal.put(("dispatch", action_id, {
            "operation": "navigate", "risk": "write", "producer": "verifier", "gate_seq": gate_seq,
            "observation_id": self.inner.report.get("observation_id"), "policy_revision": self.observed_revision,
            "target_id": "reopen", "destination": f"{parts.scheme}://{parts.netloc}{parts.path or '/'}"}))
        try:
            self.inner.reopen()
        except Exception:
            self.journal.put(("effect_unknown", action_id))
            return False
        else:
            self.journal.put(("driver_return", action_id, True))
        finally:
            self.gate.settle(action_id)
        try:
            self.inner.observe()
        except Exception:
            return False
        effect, self.inner.effect_report = self.inner.effect_report, None
        self.journal.observed(self.inner.report, effect)
        return True

    def verification_observation(self):
        """A last read-only look after the loop; it is not a decision cycle and spends no step."""
        self.gate._acquire()
        try:
            self.gate._check()
        finally:
            self.gate.lock.release()
        obs = self.inner.observe()
        effect, self.inner.effect_report = self.inner.effect_report, None
        self.journal.observed(self.inner.report, effect)
        return obs

    def execute(self, action, params):
        self.journal.put(("proposal", {"action": action}))
        prepared = None
        try:
            prepared = self.inner.prepare(action, params)
            reason = None
        except RuntimeStop as exc:
            reason = exc.reason
        if reason == "stale_target":
            self._stale_streak += 1
            self.journal.put(("rejected", "stale_target"))
            self.journal.put(("reobserve", "stale_target"))
            if self._stale_streak >= STALE_LIMIT:
                raise self._stop("stale_loop")
            return SkippedAction()
        if reason:
            self.journal.put(("rejected", reason))
            raise self._stop(reason)
        self._stale_streak = 0
        operation = prepared.operation
        if not self.gate.permitted(operation):
            # Withdrawn after the option was offered; nothing has been dispatched.
            self.journal.put(("rejected", "policy_denied"))
            raise self._stop("policy_denied")
        if self.uncertain and prepared.risk == "write":
            # An earlier write's effect is unknown and nothing here can settle it (V0.6 section 9).
            # Looking again (scroll, wait, switch_tab) is still allowed; changing things is not.
            self.journal.put(("rejected", "effect_unknown"))
            raise self._stop("effect_unknown")
        state_key = self.inner.state_key(prepared)
        if operation != "wait" and state_key in self._done_in_state:
            # The same action in the same essential state is not repeated (V0.6 section 8). Waiting again is
            # what waiting is for; the no-progress count bounds it.
            self.journal.put(("rejected", "repeated_action"))
            self.journal.put(("reobserve", "repeated_action"))
            return SkippedAction()
        # A click known to send a GET (a link, a GET form) changes nothing on the server by the protocol's
        # contract; the document-level guard is for writes that may (section 9). The state-level guard above
        # still refuses it on an unchanged page.
        request = getattr(prepared.target, "request", None)
        mutation_key = (self.inner.mutation_key(prepared)
                        if prepared.risk == "write" and not (request and request[0] == "GET") else None)
        if mutation_key is not None and mutation_key in self._mutations:
            # A write's server effect is unknown in this build, and a write changes the page (the saved
            # note shows up), so a new page state does not make the same Save a new operation (V0.6
            # section 9). A real Jev run saved one note nine times before this check existed.
            self.journal.put(("rejected", "repeated_mutation"))
            self.journal.put(("reobserve", "repeated_mutation"))
            return SkippedAction()
        classification = "text" if prepared.action == "type_text" else "nontext"
        try:
            action_id, gate_seq = self.gate.commit_dispatch_seq(classification, operation=operation,
                                                                revision=self.observed_revision)
            if self.gate.slot_sink is not None:
                self.gate.slot_sink.commit(action_id, classification, prepared.slot_ref)
        except RuntimeStop as exc:
            if exc.reason == "stale_policy":
                self.journal.put(("reobserve", "stale_policy"))
                return SkippedAction()
            self.journal.put(("rejected", exc.reason))
            raise self._stop(exc.reason) from None
        descriptor = prepared.descriptor()
        self._done_in_state.add(state_key)
        if mutation_key is not None:
            self._mutations.add(mutation_key)
            descriptor["mutation_key"] = mutation_key
        self.journal.put(("dispatch", action_id, {**descriptor, "gate_seq": gate_seq,
                                                  "policy_revision": self.observed_revision,
                                                  "target_id": state_key}))
        try:
            result = self.inner.execute_prepared(prepared, action_id)
        except RuntimeStop as exc:
            self.uncertain = self.uncertain or prepared.risk == "write"
            self.journal.put(("effect_unknown", action_id))
            if exc.reason != "effect_unknown":
                raise self._stop(exc.reason) from None
            return _DriverFailure()
        except Exception:
            # An exception does not prove the page was untouched. The loop may look again but not act.
            self.uncertain = self.uncertain or prepared.risk == "write"
            self.journal.put(("effect_unknown", action_id))
            return _DriverFailure()
        else:
            if not result.ok and prepared.risk == "write":
                self.uncertain = True
            self.journal.put(("driver_return", action_id, bool(result.ok)))
            return result
        finally:
            self.gate.settle(action_id)

    def close(self):
        self.inner.close()


class _DriverFailure:
    ok = False
    text = "The driver reported an error; the effect is unknown."
    artifacts = ()
    terminal = False

    def to_dict(self):
        return {"ok": False, "text": self.text, "artifacts": [], "terminal": False, "fields": {}}


# Providers whose every request is a model call, reserved against `max_model_calls` before it is sent.
MODEL_PROVIDERS = ("jev", "laya")


class JournalingProvider:
    """Writes `decision_requested` / `decision_received` around one provider request."""

    def __init__(self, inner, journal: Journal, requested_model: str | None):
        self.inner, self.journal, self.requested_model = inner, journal, requested_model
        self.served_models: list[str] = []

    @property
    def _client(self):
        return self.inner._client

    def decide(self, state, questions: dict):
        self.journal.emit("decision_requested", observation_id=self.journal.current_observation,
                          requested_model=self.requested_model,
                          questions=len(questions),
                          options={k: len(q.get("criteria") or {}) for k, q in questions.items()
                                   if q.get("type") == "choice"})
        try:
            decision = self.inner.decide(state, questions)
        except Exception as exc:
            self.journal.emit("decision_failed", error=type(exc).__name__, status=getattr(exc, "status", None),
                              protocol=str(exc).startswith("protocol:"))
            raise
        answers = decision.answers if type(decision.answers) is dict else {}
        chosen = (answers.get("next_action") or {}).get("choice")
        params = {key.split("__", 1)[1]: (answers.get(key) or {}).get("choice")
                  for key in answers if chosen and key.startswith(f"{chosen}__") and not key.endswith("__stated")}
        if decision.model not in self.served_models:
            self.served_models.append(decision.model)
        self.journal.emit("decision_received", observation_id=self.journal.current_observation,
                          requested_model=self.requested_model, served_model=decision.model,
                          request_id=decision.request_id, latency_ms=decision.latency_ms, usage=decision.usage,
                          action=chosen, params=params,
                          confidence=(answers.get("next_action") or {}).get("confidence"),
                          goal_reached=(answers.get("goal_reached") or {}).get("noul"),
                          **({"translated": decision.raw["translated"]}
                             if isinstance(getattr(decision, "raw", None), dict) and "translated" in decision.raw else {}))
        return decision

    def close(self):
        if hasattr(self.inner, "close"):
            self.inner.close()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def processes_using(path: str) -> list[int]:
    """PIDs whose command line names `path` (Linux): the browser we started and its children."""
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) != os.getpid():
            try:
                if path.encode() in (entry / "cmdline").read_bytes():
                    found.append(int(entry.name))
            except OSError:
                continue
    return found


class BrowserRun:
    """Runs one attempt and returns a `browser-result/0.1`."""

    def __init__(self, task: BrowserTask, *, chrome: str | None, provider_factory, action_provider: str,
                 out_dir: Path | None = None, sandbox: bool = True, headless: bool = True,
                 attempt_id: str | None = None, checks: dict | None = None, chain=None):
        self.task, self.chrome = task, chrome
        self.chain = chain             # a TaskChain when run by BrowserCore (handoff in the same session)
        self.retained = False          # the session outlives this attempt, handed to the chain
        # Registered checks come from the service's configuration, never from the task (V0.6 section 11).
        self.checks = dict(checks or {})
        self._claimed: list = []
        self.provider_factory, self.action_provider = provider_factory, action_provider
        self.sandbox, self.headless = sandbox, headless
        self.attempt_id = attempt_id or uuid.uuid4().hex
        self.out_dir = Path(out_dir) if out_dir else None
        self._cancel = threading.Event()
        self.adapter: GenericBrowserAdapter | None = None
        self.config_id: str | None = None
        self.requested_model: str | None = None
        self.outcome: dict = {}
        self.provider = None
        self.controlled = None
        self.assist_left = 0

    def cancel(self) -> None:
        self._cancel.set()

    # ── preflight ──
    def _preflight(self) -> str | None:
        for module in ("browser_use", "systemone_harness"):
            try:
                importlib.metadata.version(module.replace("_", "-"))
            except importlib.metadata.PackageNotFoundError:
                return f"{module}_not_installed"
        if not self.chrome or not Path(self.chrome).exists():
            return "chromium_not_found"
        return None

    def _components(self) -> dict:
        def version(name):
            try:
                return importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                return None

        soh_commit = None
        try:
            import systemone_harness

            root = Path(systemone_harness.__file__).resolve().parent.parent
            soh_commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                                        text=True, timeout=5).stdout.strip() or None
        except Exception:
            pass
        browser_use_commit = None
        try:  # present when installed from a git URL: the commit that was actually installed
            direct = json.loads(importlib.metadata.distribution("browser-use").read_text("direct_url.json") or "{}")
            browser_use_commit = (direct.get("vcs_info") or {}).get("commit_id")
        except Exception:
            pass
        return {"reflexmesh": version("reflexmesh"),
                "browser_use": {"version": version("browser-use"), "commit": browser_use_commit,
                                "pinned_commit": BROWSER_USE_PIN, "matches_pin": browser_use_commit == BROWSER_USE_PIN},
                "systemone_harness": {"version": version("systemone-harness"), "commit": soh_commit,
                                      "pinned_commit": SOH_PIN, "matches_pin": soh_commit == SOH_PIN},
                "chromium": chrome_version(self.chrome) if self.chrome and Path(self.chrome).exists() else None,
                "chromium_sandbox": self.sandbox, "headless": self.headless,
                "decision_provider": {"kind": self.action_provider, "config_id": self.config_id,
                                      "requested_model": self.requested_model},
                "capabilities": dict(CAPABILITIES)}

    # ── the attempt ──
    def run(self) -> dict:
        problem = self._begin()
        if problem is not None:
            if self.chain is not None:
                # The token is already spent: a refused continuation must not leave the browser open unleased.
                self.chain.close()
            return problem
        worker = threading.Thread(target=self._work, name="reflexmesh-browser-loop", daemon=True)
        worker.start()
        cleanup_notes = self._supervise(worker)
        return self._finish(self.started, cleanup_notes)

    def _begin(self, *, provider_needed: bool = True) -> dict | None:
        """Everything before the browser is touched: acceptance, provider, claims, gate, journal. Returns a
        refusal result, or None when the attempt may start."""
        task, chain = self.task, self.chain
        started = self.started = chain.started if chain is not None else time.monotonic()
        unsupported = task.criteria_unsupported(frozenset(self.checks))
        if unsupported:
            return self._refusal("blocked", "unsupported_criterion", {"criteria": list(unsupported)})
        if task.unsupported_constraints:
            return self._refusal("blocked", "unsupported_constraint", {"constraints": list(task.unsupported_constraints)})
        problem = self._preflight()
        if problem:
            return self._refusal("blocked", "executor_unavailable", {"detail": problem})
        provider = None
        if provider_needed:
            try:
                provider, self.config_id, self.requested_model = self.provider_factory()
            except ProviderUnavailable as exc:
                return self._refusal("blocked", "executor_unavailable", {"detail": "decision_provider_unavailable",
                                                                          "message": str(exc)})
        else:
            self.config_id = "caller:llm_assistance"
        if chain is not None:
            self._claimed = list(chain.claimed)
        used = sorted({c.args["check_id"] for c in task.criteria if c.predicate == "registered_check"} -
                      {check.check_id for check in self._claimed})
        for check_id in used:
            # The claim comes before the first navigation: everything the site records after it is this attempt's
            # (the chain's, when there is one: the claim lasts as long as the session).
            try:
                self.checks[check_id].claim(chain.chain_id if chain is not None else self.attempt_id, timeout=2.0)
            except Exception as exc:
                self._release_checks()
                return self._refusal("blocked", "unsupported_criterion",
                                     {"criteria": [c.id for c in task.criteria if c.args.get("check_id") == check_id],
                                      "check_id": check_id, "detail": str(exc) if str(exc).startswith("check_") else
                                      "check_unavailable"})
            self._claimed.append(self.checks[check_id])
            if chain is not None:
                chain.claimed.append(self.checks[check_id])
        if self.out_dir is not None:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        ctx = mp.get_context("fork")
        view = AttemptTask(task, self.attempt_id, chain.remaining_limits() if chain is not None else None)
        self.view = view
        self.registry = SlotRegistry.from_task(view, self.attempt_id)
        self.slot_evidence = SlotEvidence(ctx, view, self.attempt_id, registry=self.registry)
        self.gate = AttemptGate(ctx, view, started, slot_registry=self.registry, slot_sink=self.slot_evidence.sink)
        self.slot_evidence.bind_dispatch_counter(self.gate.dispatches)
        producer = self.action_provider if self.action_provider in MODEL_PROVIDERS + ("llm",) else "script"
        self.journal = Journal(task, self.attempt_id, self.out_dir / "trace.jsonl" if self.out_dir else None, producer)
        self.journal.emit("task_accepted", task_id=task.task_id, revision=task.revision,
                          permissions=sorted(task.permissions), origins=list(task.allowed_origins),
                          slots=[{"ref": s.reference, "sensitivity": s.sensitivity} for s in task.slots],
                          criteria=[{"id": c.id, "predicate": c.predicate} for c in task.criteria],
                          limits={"wall_seconds": task.limits.wall_seconds, "max_steps": task.limits.max_steps,
                                  "max_model_calls": task.limits.max_model_calls})
        self.journal.emit("attempt_started", attempt_id=self.attempt_id, provider=self.action_provider,
                          config_id=self.config_id, requested_model=self.requested_model,
                          **({"chain_id": chain.chain_id, "parent_attempt_id": chain.parent(),
                              "owner_epoch": chain.owner_epoch, "session_id": chain.session_id}
                             if chain is not None else {}))
        if chain is not None and chain.session is not None:
            self.journal.emit("owner_transferred", session_id=chain.session_id, owner_epoch=chain.owner_epoch,
                              from_attempt=chain.parent())
        if provider is not None:
            self.provider = JournalingProvider(ValidatingProvider(provider), self.journal, self.requested_model)
        self.verifier = CriteriaVerifier(task, self.attempt_id, self.journal,
                                         {check.check_id: check for check in self._claimed})
        return None

    def _environment(self) -> "BrowserControlledEnvironment":
        """The adapter (the chain's open session, or a new browser) wrapped in this attempt's gate and journal."""
        chain = self.chain
        continued = chain is not None and chain.session is not None
        if continued:
            self.adapter, chain.session = chain.session, None
        else:
            self.adapter = GenericBrowserAdapter(self.task, chrome=self.chrome, sandbox=self.sandbox,
                                                 headless=self.headless)
        # The slot ledger and the task revision are this attempt's: a continued session is rebound to them.
        self.adapter.rebind_task(self.task)
        self.adapter.bind_runtime(self.registry, self.slot_evidence.sink)
        controlled = BrowserControlledEnvironment(self.adapter, self.gate, self.journal, self.task)
        controlled.verifier = self.verifier
        controlled.continued = continued
        if chain is not None:
            controlled.restore(chain.carry)
        self.controlled = controlled
        return controlled

    def _retainable(self) -> bool:
        """Section 10: a session is handed over only after a handoff, with nothing in flight and no driver-level
        unknown effect (a write whose driver call failed); otherwise it is closed."""
        out, controlled = self.outcome, self.controlled
        handoff = out.get("kind") == "handoff" or (out.get("kind") == "stopped" and
                                                    out.get("reason") in ("no_progress", "stale_loop"))
        return (self.chain is not None and handoff and not getattr(self, "_no_transfer", False) and
                not self.chain.closed and controlled is not None and not controlled.uncertain and
                self.adapter is not None and not self.adapter.closed and not self.gate.in_flight.value)

    def _release_session(self) -> None:
        if self.chain is not None and self.controlled is not None:
            self.chain.carry = self.controlled.carry()
        self.retained = self._retainable()
        if not self.retained and self.adapter is not None:
            self.adapter.close()

    def _work(self) -> None:
        from systemone_harness.controller import Controller
        from systemone_harness.gate import Gate

        journal = self.journal

        class JournalGate(Gate):
            def judge(self, answers, compiled, space):
                verdict = super().judge(answers, compiled, space)
                journal.emit("threshold_judged", action=verdict.action, verdict=verdict.kind,
                             weakest=verdict.weakest, threshold=verdict.threshold,
                             confidence=verdict.confidence, params=verdict.params)
                return verdict

        controlled = None
        try:
            controlled = self._environment()
            space = browser_action_space(self.task.permissions)
            counting = CountingProvider(self.provider, self.gate, journal, model=self.action_provider in MODEL_PROVIDERS)
            try:
                run = Controller(space, controlled, counting, gate=JournalGate(space.gate),
                                 max_steps=math.inf, timeout_seconds=None).run(self.task.goal)
            except RuntimeStop as exc:
                if exc.reason != "criteria_met":
                    raise
                run = None
            if controlled.last_stop == "criteria_met":
                self.verifier.final(controlled)
                self.outcome = {"kind": "verify", "reason": "criteria_met"}
            elif controlled.last_stop:
                raise RuntimeStop(controlled.last_stop)
            elif run.status == "failed":
                self.outcome = {"kind": "failed", "reason": run.reason}
            elif run.status == "completed":
                controlled.verification_observation()
                self.verifier.final(controlled)
                self.outcome = {"kind": "finish", "reason": run.reason}
            else:
                self.outcome = {"kind": "handoff", "reason": run.reason}
        except RuntimeStop as exc:
            self.outcome = {"kind": "stopped", "reason": exc.reason}
        except BaseException as exc:  # the loop's failure is the attempt's; the message may hold page text
            self.outcome = {"kind": "failed", "reason": "worker_exception", "detail": type(exc).__name__}
        finally:
            try:
                self.provider.close()
            finally:
                self._release_session()

    # ── LLM assistance (section 10): one proposal at a time from the fresh finite options ──
    def start_assist(self) -> dict:
        from reflexmesh.runtime.browser_core import ASSIST_STEPS

        problem = self._begin(provider_needed=False)
        if problem is not None:
            self.chain.assist = None
            self.chain.close()
            return problem
        self.assist_left = ASSIST_STEPS
        try:
            self._environment()
            obs = self.controlled.observe()       # the action from the old handoff is never executed (section 14)
        except RuntimeStop as exc:
            return self._assist_stop(exc.reason)
        except Exception as exc:
            self.outcome = {"kind": "failed", "reason": "worker_exception", "detail": type(exc).__name__}
            return self._assist_close()
        return self._progress(obs, None)

    def _proposal(self, proposal) -> tuple[str, dict] | str:
        """A proposal names an option of the latest observation; anything else is refused before admission."""
        if type(proposal) is not dict:
            return "invalid_proposal"
        report = self.adapter.report
        if proposal.get("observation_id") != report.get("observation_id"):
            return "stale_observation"
        operation = proposal.get("operation")
        if operation in ("scroll", "wait"):
            if operation not in self.task.permissions:
                return "not_permitted"
            if operation == "wait":
                return "wait", {}
            if proposal.get("direction") not in ("up", "down"):
                return "invalid_proposal"
            return "scroll", {"direction": proposal["direction"]}
        options = {o["ref"]: o for o in report.get("option_list") or []}
        ref = proposal.get("option_ref")
        if ref not in options:
            return "unknown_option"
        if options[ref]["operation"] != operation:
            return "operation_mismatch"
        _, kind, key = ref.split(":", 2)
        if kind == "elements":
            return "click", {"element": key}
        if kind == "text_fields":
            if proposal.get("slot_ref") not in self.task.slot_references:
                return "unknown_slot"
            return "type_text", {"field": key, "value": proposal["slot_ref"]}
        if kind == "options":
            return "select_option", {"option": key}
        if kind == "tabs":
            return "switch_tab", {"tab": key}
        if kind == "history":
            return "go_back", {}
        return "invalid_proposal"

    def assist_step(self, proposal) -> dict:
        if self.assist_left <= 0:
            return self._progress(self._last_obs, {"rejected": "assistance_limit"})
        converted = self._proposal(proposal)
        if isinstance(converted, str):
            return self._progress(self._last_obs, {"rejected": converted})
        action, params = converted
        before = len(self.journal.actions)
        try:
            self.controlled.execute(action, params)
            dispatched = len(self.journal.actions) > before
            if dispatched:
                self.assist_left -= 1
            rejected = None if dispatched else next(
                (r["reason"] for r in reversed(self.journal.records) if r["kind"] == "admission_rejected"), "rejected")
            obs = self.controlled.observe()
        except RuntimeStop as exc:
            return self._assist_stop(exc.reason)
        except Exception as exc:
            self.outcome = {"kind": "failed", "reason": "worker_exception", "detail": type(exc).__name__}
            return self._assist_close()
        return self._progress(obs, {"accepted": True, "dispatched": dispatched,
                                    **({"rejected": rejected} if rejected else {})})

    def assist_end(self, why: str) -> dict:
        """`stop` or `return_to_jev`: the assisted attempt ends as a handoff; the session stays with the chain."""
        self.outcome = {"kind": "handoff", "reason": why}
        return self._assist_close()

    def assist_abandon(self, why: str) -> dict | None:
        """Lease expiry or cancel while waiting for a proposal: the attempt ends and gives nothing away."""
        if why == "cancel_requested":
            self.gate.stop("cancel_requested")
        self.outcome = {"kind": "stopped", "reason": why}
        self._no_transfer = True
        try:
            return self._assist_close()
        except Exception:
            return None

    def _assist_stop(self, reason: str) -> dict:
        if reason == "criteria_met":
            self.verifier.final(self.controlled)
            self.outcome = {"kind": "verify", "reason": "criteria_met"}
        else:
            self.outcome = {"kind": "stopped", "reason": reason}
        return self._assist_close()

    def _assist_close(self) -> dict:
        self.chain.assist = None
        self._release_session()
        return self._finish(self.started, {"worker_stopped": True, "watchdog_kill": False})

    def _progress(self, obs, last: dict | None) -> dict:
        """`browser-progress/0.1`: never completed, never a result; a new one-time token every time."""
        self._last_obs = obs
        chain, report = self.chain, self.adapter.report
        token, lease = chain.issue_token(self.attempt_id)
        parts = urlsplit(str(report.get("url") or ""))
        options = list(report.get("option_list") or [])
        options += [{"ref": None, "operation": op} for op in ("scroll", "wait") if op in self.task.permissions]
        snapshot = self.gate.snapshot()
        return {"schema_version": "browser-progress/0.1", "task_id": self.task.task_id,
                "revision": self.task.revision, "chain_id": chain.chain_id, "attempt_id": self.attempt_id,
                "phase": "awaiting_proposal",
                "observation": {"observation_id": report.get("observation_id"),
                                "page": f"{parts.scheme}://{parts.netloc}{parts.path}",
                                "title": (obs.fields or {}).get("title"), "text": obs.text,
                                "coverage": report.get("coverage")},
                "action_options": options, "text_slot_refs": list(self.task.slot_references),
                "criteria": [{k: r.get(k) for k in ("id", "status", "reason")} for r in self.verifier.result_rows()],
                "assistance_steps_remaining": self.assist_left,
                "remaining_budget": {"steps": max(0, self.view.limits.max_steps - snapshot["steps"]),
                                     "model_calls": max(0, self.view.limits.max_model_calls - snapshot["model_calls"]),
                                     "wall_seconds": round(self.gate.wall_remaining() or 0.0, 3),
                                     "handoffs": chain.remaining()["handoffs"]},
                "continuation_token": token, "expires_in_seconds": round(lease, 3),
                "parent_attempt_id": self.attempt_id, "last_proposal": last}

    def _supervise(self, worker: threading.Thread) -> dict:
        """Cancel and deadline are accepted here, at the gate, whatever the loop is waiting on."""
        notes = {"watchdog_kill": False}
        terminal_at = None
        while worker.is_alive():
            self.slot_evidence.drain()
            if self._cancel.is_set():
                self.gate.stop("cancel_requested")
            elif self.gate.deadline_expired():
                self.gate.stop("deadline")
            if self.gate.terminal.value and terminal_at is None:
                terminal_at = time.monotonic()
            if terminal_at is not None and time.monotonic() - terminal_at > GRACE_SECONDS and not notes["watchdog_kill"]:
                notes["watchdog_kill"] = True
                if self.adapter is not None:
                    self.adapter.kill()
            worker.join(0.05)
            if terminal_at is not None and time.monotonic() - terminal_at > GRACE_SECONDS + KILL_SECONDS:
                break
        notes["worker_stopped"] = not worker.is_alive()
        return notes

    def _settle_registered(self, notes: dict) -> tuple[str, str] | None:
        """Read each registered check once the browser can send nothing more; judge its criteria, settle action
        effects, and return the repeat verdict (None when no check decides it)."""
        adapter = self.adapter
        stopped = bool(notes.get("worker_stopped")) and (adapter is None or adapter.closed)
        if not self._claimed or not stopped:
            return None
        evidence, verdicts = {}, []
        for check in self._claimed:
            try:
                evidence[check.check_id] = check.evidence(timeout=min(2.0, max(0.1, self.gate.wall_remaining() or 2.0)))
            except Exception:
                evidence[check.check_id] = None
        self.verifier.settle_registered(evidence)
        for check in self._claimed:
            if evidence[check.check_id] is None:
                verdicts.append(None)
                continue
            rows = self._chain_actions()   # earlier attempts' submits are this claim's requests too
            verdict = check.settle(rows, evidence[check.check_id], browser_stopped=True)
            verdicts.append(verdict)
            self.journal.emit("effects_settled", check_id=check.check_id, repeats=verdict[0] if verdict else "unknown",
                              applied=sum(1 for a in rows if a["effect"] == "applied"),
                              not_applied=sum(1 for a in rows if a["effect"] == "not_applied"),
                              unknown=sum(1 for a in rows if a["effect"] == "unknown"))
        failed = [v for v in verdicts if v and v[0] == "fail"]
        if failed:
            return failed[0]
        if verdicts and all(v and v[0] == "pass" for v in verdicts):
            return verdicts[0]
        return None

    def _chain_actions(self) -> list[dict]:
        """The effect registry (section 9): every action of the chain, this attempt's last."""
        return (self.chain.actions if self.chain is not None else []) + self.journal.actions

    def _constraint_rows(self, snapshot, coverage, repeats) -> list[dict]:
        """The six runtime constraints: the shared assessors, plus this path's own evidence for ownership (the
        browser session it launched) and repeats (a registered source that saw every request)."""
        rows = assess_constraints(self.view, snapshot, self.attempt_id, slot_evidence=self.slot_evidence,
                                  coverage=coverage, ownership=None, dispatches=self.journal.actions, revocations=[],
                                  driver_outcomes=self.journal.outcomes, effect_snapshot=None)
        by_id = {row["id"]: row for row in rows}
        ownership = self._ownership_row()
        if ownership is not None:
            by_id["runtime.ownership"].update(ownership)
        gate_repeats = by_id["runtime.no_uncertain_repeats"]
        if repeats is not None and gate_repeats["status"] != "fail":
            gate_repeats.update(status=repeats[0], reason=repeats[1], observed_at=time.time(),
                                evidence_refs=[f"runtime:{self.attempt_id}:no_uncertain_repeats"] +
                                [f"check:{c.check_id}:{c.claim_id}" for c in self._claimed],
                                scope="registered_source")
        if self.chain is not None:
            # A confirmed failure stays with the chain through every later attempt (section 11).
            for cid, failed in self.chain.failed_constraints.items():
                if by_id[cid]["status"] != "fail":
                    by_id[cid].update(status="fail", observed_at=failed["observed_at"],
                                      evidence_refs=list(failed["evidence_refs"]),
                                      reason="Confirmed earlier in this chain: " + failed["reason"])
        return rows

    def _ownership_row(self) -> dict | None:
        """V0.6 section 11, ownership within its stated scope: the browser session this attempt launched.
        CDP on a loopback port has no authentication, so a client connecting between two looks is not excluded;
        the reason says so."""
        adapter = self.adapter
        if adapter is None or adapter.session is None:
            return None
        facts, seen = adapter.session, adapter.controllers
        ref = [f"runtime:{self.attempt_id}:ownership"]
        scope = "browser_session_launched_by_attempt" if self.chain is None else "browser_session_of_chain"
        if seen["foreign_pids"] or seen["session_changed"]:
            return {"status": "fail", "observed_at": time.time(), "evidence_refs": ref, "scope": scope,
                    "reason": ("Another process held a connection to this attempt's browser debugging port."
                               if seen["foreign_pids"] else "The browser process changed during the attempt.")}
        leftover = processes_using(adapter.profile_dir) if adapter.closed else None
        missing = [why for bad, why in (
            (not facts.get("pid") or not facts.get("child_of_runtime"), "the browser is not a known child process"),
            (not seen["samples"] or not seen["own_seen"], "no observation saw this runtime on the debugging port"),
            (seen["unattributed"] or seen["errors"], "a connection's owner could not be read"),
            (not adapter.closed or leftover, "the browser is not closed")) if bad]
        if missing:
            return {"status": "unknown", "observed_at": None, "evidence_refs": [], "scope": scope,
                    "reason": "Browser session ownership is not established: " + "; ".join(missing) + "."}
        return {"status": "pass", "observed_at": time.time(), "evidence_refs": ref, "scope": scope,
                "reason": (f"The browser was launched by this {'attempt' if self.chain is None else 'chain'} "
                           f"(a child process with its own profile, verified "
                           f"in use), stayed the same process, had only this runtime on its debugging port at each of "
                           f"{seen['samples']} observations, and was closed with no process left. A client connecting "
                           f"between two observations is not excluded.")}

    def _release_checks(self) -> None:
        for check in self._claimed:
            try:
                check.release(timeout=1.0)
            except Exception:
                pass
        self._claimed = []

    # ── the result ──
    def _refusal(self, status: str, reason: str, detail: dict) -> dict:
        result = {"schema_version": RESULT_SCHEMA, "task_id": self.task.task_id, "revision": self.task.revision,
                "chain_id": self.attempt_id, "attempt_id": self.attempt_id, "parent_attempt_id": None,
                "attempt_status": status, "stop_reason": reason, "task_outcome": "unknown",
                "execution_outcome": "not_started", "outputs": [], "verification": [], "actions": [],
                "detail": detail, "handoff": None, "components": self._components(),
                "cleanup": {"status": "not_started"}, "trace_ref": None}
        if self.out_dir is not None:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            (self.out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
        return result

    def _terminal(self) -> tuple[str, str, dict | None]:
        """(attempt_status, stop_reason, handoff reason) from how the loop ended."""
        out = self.outcome
        if not out:  # the loop never reported: a hang that the watchdog ended
            terminal = self.gate.terminal.value
            return (("cancelled", "cancel_requested", None) if terminal == 1 else
                    ("incomplete", "deadline", None) if terminal == 2 else ("failed", "executor_error", None))
        if out["kind"] == "stopped":
            reason = out["reason"]
            if reason in ("stale_loop", "no_progress", "lease_expired"):
                return "incomplete", "needs_handoff", reason
            if reason in STOP_CODES:
                return STOP_CODES[reason], reason, None
            return "failed", "executor_error", None
        if out["kind"] == "failed":
            return "failed", ("protocol_error" if out["reason"] == "provider_error" else "executor_error"), None
        if out["kind"] == "handoff":
            return "incomplete", "needs_handoff", HANDOFF_REASONS.get(out["reason"], out["reason"])
        return "verifying", out["reason"] if out["kind"] == "verify" else "finish", None

    def _finish(self, started: float, notes: dict) -> dict:
        task, gate, journal, chain = self.task, self.gate, self.journal, self.chain
        parent = chain.parent() if chain is not None else None
        terminal_at = time.monotonic()
        status, reason, handoff_reason = self._terminal()
        try:
            gate.seal_dispatches()
        except RuntimeStop:
            pass
        self.slot_evidence.drain()
        repeats = self._settle_registered(notes)
        # Criteria as the runtime's verifier last judged them. After a stop other than finish or met criteria
        # they are late information: the first terminal wins and they never turn the attempt into completed.
        criteria = self.verifier.result_rows()
        constraints = ConstraintAssessments()
        try:
            snapshot = gate.snapshot()
            coverage = gate.slot_snapshot()
        except RuntimeStop:
            snapshot = coverage = None
        constraints.update(self._constraint_rows(snapshot, coverage, repeats))
        expected_generation = None
        if status == "verifying":
            verdicts = [row["status"] for row in criteria]
            dispatches = self._chain_actions()
            settled = (snapshot is not None and not snapshot["in_flight"] and
                       len(journal.actions) == snapshot["dispatches"] and
                       all(row["effect"] != "unknown" for row in dispatches))
            if constraints.has_fail():
                status, reason = "failed", "constraint_violated"
            elif "fail" in verdicts:
                status, reason = "failed", "postcondition_failed"
            elif verdicts and all(v == "pass" for v in verdicts) and constraints.all_pass() and settled:
                # Section 11: every criterion and constraint passes and no effect is left unknown.
                status = "completed"
                reason = ("already_satisfied" if all(row["bootstrap"] or row["producer"] == "verifier"
                                                     for row in dispatches) else "verified")
                # Completion is accepted only if no slot evidence moved since it was assessed.
                expected_generation = gate.generation.value
            else:
                status, reason = "incomplete", "verification_unknown"
        elif constraints.has_fail() and status not in ("cancelled", "failed"):
            # Section 13: a confirmed constraint violation ends the attempt as failed, whatever stopped the loop;
            # in particular a session with a second controller is never handed over.
            status, reason, handoff_reason = "failed", "constraint_violated", None
        accepted = None
        try:
            accepted = gate.finish(expected_generation=expected_generation)
        except RuntimeStop as exc:
            if exc.reason == "evidence_changed":
                status, reason = "incomplete", "verification_unknown"
                try:
                    accepted = gate.finish()
                except RuntimeStop:
                    status, reason = "failed", "executor_error"
            else:
                status, reason = "failed", "executor_error"
        if accepted == 1:
            status, reason = "cancelled", "cancel_requested"
        elif accepted == 2 and status != "cancelled":
            status, reason = "incomplete", "deadline"
        refused_transfer = None
        if self.retained and (status, reason) != ("incomplete", "needs_handoff"):
            refused_transfer = "attempt_not_handed_off"
        elif self.retained and (chain.closed or chain.handoffs_used >= chain.root_limits.max_handoffs):
            refused_transfer = "chain_closed" if chain.closed else "handoff_limit"
        if refused_transfer:
            self.retained = False
            self.adapter.close()
        journal.emit("terminal", attempt_status=status, stop_reason=reason, handoff_reason=handoff_reason)
        cleanup = self._cleanup()
        try:
            snapshot = gate.snapshot()
            # The terminal transition now owns its gate number: assess once more with it counted.
            constraints.update(self._constraint_rows(snapshot, gate.slot_snapshot(), repeats))
        except RuntimeStop:
            pass
        limits = self.view.limits
        steps, calls = snapshot["steps"], snapshot["model_calls"]
        actions = journal.actions
        continuation = None
        if chain is not None:
            chain.record(self.attempt_id, snapshot, actions, constraints.rows())
            if self.retained:
                chain.session = self.adapter
                token, lease = chain.issue_token(self.attempt_id)
                journal.emit("handoff_created", session_id=chain.session_id, owner_epoch=chain.owner_epoch,
                             lease_seconds=round(lease, 3))
                continuation = {"allowed": True, "token": token, "expires_in_seconds": round(lease, 3),
                                "parent_attempt_id": self.attempt_id, "modes": ["jev", "llm"],
                                "session_id": chain.session_id}
            else:
                continuation = {"allowed": False, "reason": refused_transfer or "session_not_transferable"}
                if chain.assist is None:
                    chain.close()
        else:
            # A one-shot attempt holds no session after it returns: nothing to resume (section 14).
            continuation = {"allowed": False, "reason": "no_session_service"}
        unsettled = any(a["effect"] == "unknown" for a in actions) or bool(snapshot["in_flight"])
        outcome = ("fail" if constraints.has_fail() or (status == "failed" and reason in
                                                       ("postcondition_failed", "constraint_violated"))
                   else "pass" if status == "completed" else "unknown")
        result = {
            "schema_version": RESULT_SCHEMA, "task_id": task.task_id, "revision": task.revision,
            "chain_id": chain.chain_id if chain is not None else standalone_chain(self.attempt_id)["root_attempt_id"],
            "attempt_id": self.attempt_id,
            "parent_attempt_id": parent, "attempt_status": status, "stop_reason": reason, "task_outcome": outcome,
            "execution_outcome": ("unknown" if unsettled else "returned" if snapshot["dispatches"]
                                  else "error" if status == "failed" else "not_started"),
            "outputs": list(self.verifier.outputs.values()), "verification": criteria + constraints.rows(),
            "actions": actions, "budget": {
                "steps": steps, "model_calls": calls, "dispatches": snapshot["dispatches"],
                "limits": {"wall_seconds": limits.wall_seconds, "max_steps": limits.max_steps,
                           "max_model_calls": limits.max_model_calls, "max_handoffs": limits.max_handoffs},
                "remaining_steps": max(0, limits.max_steps - steps),
                "remaining_model_calls": max(0, limits.max_model_calls - calls),
                "remaining_wall_seconds": round(gate.wall_remaining(), 3),
                "model_call_limit_scope": "locally_controlled_provider_requests",
                "elapsed_seconds": round(terminal_at - started, 3)},
            "handoff": (self._handoff(handoff_reason, reason, criteria, continuation)
                        if status == "incomplete" else None),
            "components": self._components(), "cleanup": cleanup, "watchdog": notes,
            "decision": {"kind": self.action_provider, "requested_model": self.requested_model,
                         "served_models": list(self.provider.served_models) if self.provider else [],
                         "config_id": self.config_id},
            **({"session_id": chain.session_id, "owner_epoch": chain.owner_epoch,
                "chain_budget": {"used": dict(chain.used), "remaining": chain.remaining()}}
               if chain is not None else {}),
            "trace_ref": "trace.jsonl" if self.out_dir else None,
            "evidence_refs": [f"runtime:{self.attempt_id}:{n}" for n in
                              ("permissions", "dispatch_ordering", "budget")],
            "timing": {"terminal_after_seconds": round(terminal_at - started, 3),
                       "total_seconds": round(time.monotonic() - started, 3)},
            "slot_evidence": self.slot_evidence.export()}
        if status == "failed" and self.outcome.get("detail"):
            result["detail"] = {"exception": self.outcome["detail"]}
        if self.outcome.get("kind") == "stopped" and status == "failed":
            result["detail"] = {"stop": self.outcome["reason"]}
        if chain is None:
            self._release_checks()
        self.slot_evidence.close()
        journal.close()
        if self.out_dir is not None:
            # The continuation token is a bearer right over the open session: the caller gets it in the returned
            # result, the file on disk does not keep it.
            stored = json.loads(json.dumps(result, default=str))
            kept = ((stored.get("handoff") or {}).get("continuation") or {})
            if "token" in kept:
                kept["token"] = "[withheld]"
            (self.out_dir / "result.json").write_text(json.dumps(stored, ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
        return result

    def _handoff(self, handoff_reason: str | None, stop_reason: str, criteria: list[dict],
                 continuation: dict | None) -> dict | None:
        """The package of section 10: enough for a caller to decide the next step, never a slot value."""
        if handoff_reason is None and stop_reason not in ("effect_unknown", "verification_unknown"):
            return None
        last = self.journal.last_observation
        chain = self.chain
        report = self.adapter.report if self.adapter is not None else {}
        return {
            "schema_version": "browser-handoff/0.1",
            "reason": handoff_reason or stop_reason, "task_id": self.task.task_id, "revision": self.task.revision,
            "chain_id": chain.chain_id if chain is not None else self.attempt_id, "attempt_id": self.attempt_id,
            "parent_attempt_id": chain.attempts[-2] if chain is not None and len(chain.attempts) > 1 else None,
            "observation_ref": (last or {}).get("observation_id"), "page": (last or {}).get("page"),
            "available_actions": list(report.get("option_list") or []),
            "operations": [{"id": a["id"], "attempt_id": a.get("attempt_id", self.attempt_id),
                            "operation": a["operation"], "producer": a.get("producer"), "option_ref": a["option_ref"],
                            "slot_ref": a["slot_ref"], "driver_outcome": a["driver_outcome"], "effect": a["effect"]}
                           for a in (chain.actions if chain is not None else self.journal.actions)],
            "unresolved_effects": [a["id"] for a in (chain.actions if chain is not None else self.journal.actions)
                                   if a["effect"] == "unknown" and a["operation"] != "navigate"],
            "criteria": [{k: row.get(k) for k in ("id", "status", "reason")} for row in criteria],
            "permissions": sorted(self.task.permissions), "text_slot_refs": list(self.task.slot_references),
            "owner": ({"session_id": chain.session_id, "owner_epoch": chain.owner_epoch, "session_open": self.retained}
                      if chain is not None else {"session_open": False}),
            "remaining": chain.remaining() if chain is not None else None,
            "continuation": continuation}

    def _cleanup(self) -> dict:
        adapter = self.adapter
        if adapter is None:
            return {"status": "not_created"}
        if self.retained:
            # Handed to the chain: the browser stays open under its lease; the chain closes it.
            return {"status": "retained", "session_id": self.chain.session_id, "profile_removed": False}
        profile = adapter.profile_dir
        try:
            adapter.close()
        except Exception:
            pass
        leftover = processes_using(profile)
        forced = False
        if leftover:
            for pid in leftover:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            deadline = time.monotonic() + KILL_SECONDS
            while time.monotonic() < deadline and any(_pid_alive(p) for p in leftover):
                time.sleep(0.05)
            forced = True
            leftover = [p for p in processes_using(profile) if _pid_alive(p)]
        shutil.rmtree(profile, ignore_errors=True)
        removed = not Path(profile).exists()
        status = "unknown" if leftover or not removed else ("forced" if forced else "closed")
        return {"status": status, "profile_removed": removed, "browser_processes_left": len(leftover)}
