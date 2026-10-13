"""One controlled attempt of a `browser-task/0.1` (V0.6, M1-M2).

The loop is SystemOneHarness's: observe, ask a decision provider, gate, act. This module adds
what ReflexMesh owns around it: every observation spends a step, every model request spends a
call, every browser command passes the shared dispatch gate, and a redacted journal records
the chain options -> decision -> admission -> dispatch -> driver -> observed effect.

What this build does not do, stated here so a green run is not misread:
  * no process isolation (the loop runs in a thread; a watchdog kills the browser on a hang),
  * no verifier for task criteria (M4), so a finished run is `incomplete/verification_unknown`,
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

RESULT_SCHEMA = "browser-result/0.1"
SOH_PIN = "ab8e8f08b4a6268c0633a474423d556599ee06a4"
BROWSER_USE_PIN = "5c892e013a73e6622e6f50336e1eb0aa2c4405f2"
STALE_LIMIT = 3
HANDOFF_REASONS = {"no_confident_action": "no_confident_action", "escalation_requested": "escalation_requested",
                   "repeated_action": "no_progress", "stale_loop": "stale_loop"}
PREDICATE_PENDING = "No verifier for this predicate exists in this build (V0.6 M4); the criterion stays unknown."
MIN_SCRUBBED_NORMAL = 4


class AttemptTask:
    """The accepted task plus the attempt identity that the gate, slot ledger and constraint rows bind to."""

    def __init__(self, task: BrowserTask, attempt_id: str):
        self._task = task
        self.run_id = attempt_id

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
                "target": desc.get("target"), "slot_ref": desc.get("slot_ref"),
                "observation_id": desc.get("observation_id"), "producer": self.producer,
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


class BrowserControlledEnvironment:
    """SystemOneHarness `Environment` whose every command passes admission and the dispatch gate."""

    def __init__(self, inner: GenericBrowserAdapter, gate: AttemptGate, journal: Journal, task: BrowserTask):
        self.inner, self.gate, self.journal, self.task = inner, gate, journal, task
        self.last_stop: str | None = None
        self.uncertain = False
        self.observed_revision: int | None = None
        self._stale_streak = 0

    def _stop(self, reason: str) -> RuntimeStop:
        self.last_stop = reason
        return RuntimeStop(reason)

    def reset(self, goal):
        """The first navigation is a dispatch like any other: permission, step, gate, journal."""
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
        return obs

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
        self.journal.put(("dispatch", action_id, {**descriptor, "gate_seq": gate_seq,
                                                  "policy_revision": self.observed_revision,
                                                  "target_id": self.inner.state_key(prepared)}))
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
                 attempt_id: str | None = None):
        self.task, self.chrome = task, chrome
        self.provider_factory, self.action_provider = provider_factory, action_provider
        self.sandbox, self.headless = sandbox, headless
        self.attempt_id = attempt_id or uuid.uuid4().hex
        self.out_dir = Path(out_dir) if out_dir else None
        self._cancel = threading.Event()
        self.adapter: GenericBrowserAdapter | None = None
        self.config_id: str | None = None
        self.requested_model: str | None = None
        self.outcome: dict = {}

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
        task = self.task
        started = time.monotonic()
        if task.unsupported_criteria:
            return self._refusal("blocked", "unsupported_criterion", {"criteria": list(task.unsupported_criteria)})
        if task.unsupported_constraints:
            return self._refusal("blocked", "unsupported_constraint", {"constraints": list(task.unsupported_constraints)})
        problem = self._preflight()
        if problem:
            return self._refusal("blocked", "executor_unavailable", {"detail": problem})
        try:
            provider, self.config_id, self.requested_model = self.provider_factory()
        except ProviderUnavailable as exc:
            return self._refusal("blocked", "executor_unavailable", {"detail": "decision_provider_unavailable",
                                                                      "message": str(exc)})
        if self.out_dir is not None:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        ctx = mp.get_context("fork")
        view = AttemptTask(task, self.attempt_id)
        self.view = view
        self.registry = SlotRegistry.from_task(view, self.attempt_id)
        self.slot_evidence = SlotEvidence(ctx, view, self.attempt_id, registry=self.registry)
        self.gate = AttemptGate(ctx, view, started, slot_registry=self.registry, slot_sink=self.slot_evidence.sink)
        self.slot_evidence.bind_dispatch_counter(self.gate.dispatches)
        self.journal = Journal(task, self.attempt_id, self.out_dir / "trace.jsonl" if self.out_dir else None,
                               self.action_provider if self.action_provider in MODEL_PROVIDERS else "script")
        self.journal.emit("task_accepted", task_id=task.task_id, revision=task.revision,
                          permissions=sorted(task.permissions), origins=list(task.allowed_origins),
                          slots=[{"ref": s.reference, "sensitivity": s.sensitivity} for s in task.slots],
                          criteria=[{"id": c.id, "predicate": c.predicate} for c in task.criteria],
                          limits={"wall_seconds": task.limits.wall_seconds, "max_steps": task.limits.max_steps,
                                  "max_model_calls": task.limits.max_model_calls})
        self.journal.emit("attempt_started", attempt_id=self.attempt_id, provider=self.action_provider,
                          config_id=self.config_id, requested_model=self.requested_model)
        self.provider = JournalingProvider(ValidatingProvider(provider), self.journal, self.requested_model)
        worker = threading.Thread(target=self._work, name="reflexmesh-browser-loop", daemon=True)
        worker.start()
        cleanup_notes = self._supervise(worker)
        return self._finish(started, cleanup_notes)

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
            self.adapter = GenericBrowserAdapter(self.task, chrome=self.chrome, sandbox=self.sandbox,
                                                 headless=self.headless)
            self.adapter.bind_runtime(self.registry, self.slot_evidence.sink)
            space = browser_action_space(self.task.permissions)
            controlled = BrowserControlledEnvironment(self.adapter, self.gate, journal, self.task)
            counting = CountingProvider(self.provider, self.gate, journal, model=self.action_provider in MODEL_PROVIDERS)
            run = Controller(space, controlled, counting, gate=JournalGate(space.gate),
                             max_steps=math.inf, timeout_seconds=None).run(self.task.goal)
            if controlled.last_stop:
                raise RuntimeStop(controlled.last_stop)
            if run.status == "failed":
                self.outcome = {"kind": "failed", "reason": run.reason}
            elif run.status == "completed":
                controlled.verification_observation()
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
                if controlled is not None:
                    controlled.close()
                elif self.adapter is not None:
                    self.adapter.close()

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
            if reason == "stale_loop":
                return "incomplete", "needs_handoff", "stale_loop"
            if reason in STOP_CODES:
                return STOP_CODES[reason], reason, None
            return "failed", "executor_error", None
        if out["kind"] == "failed":
            return "failed", ("protocol_error" if out["reason"] == "provider_error" else "executor_error"), None
        if out["kind"] == "handoff":
            return "incomplete", "needs_handoff", HANDOFF_REASONS.get(out["reason"], out["reason"])
        return "verifying", "finish", None

    def _finish(self, started: float, notes: dict) -> dict:
        task, gate, journal = self.task, self.gate, self.journal
        terminal_at = time.monotonic()
        status, reason, handoff_reason = self._terminal()
        try:
            gate.seal_dispatches()
        except RuntimeStop:
            pass
        self.slot_evidence.drain()
        # Criteria: this build has no verifier, so none can pass (M4). Unknown is not a soft pass.
        criteria = [{"id": c.id, "kind": "postcondition", "predicate": c.predicate, "status": "unknown",
                     "reason": PREDICATE_PENDING, "observed_at": None, "evidence_refs": []}
                    for c in task.criteria]
        for row in criteria:
            journal.emit("criterion_assessed", id=row["id"], predicate=row["predicate"], status="unknown")
        constraints = ConstraintAssessments()
        try:
            snapshot = gate.snapshot()
            coverage = gate.slot_snapshot()
        except RuntimeStop:
            snapshot = coverage = None
        constraints.update(assess_constraints(self.view, snapshot, self.attempt_id, slot_evidence=self.slot_evidence,
                                              coverage=coverage, ownership=None, dispatches=journal.actions,
                                              revocations=[], driver_outcomes=journal.outcomes, effect_snapshot=None))
        if status == "verifying":
            if constraints.has_fail():
                status, reason = "failed", "constraint_violated"
            else:
                status, reason = "incomplete", "verification_unknown"
        try:
            accepted = gate.finish()
        except RuntimeStop:
            status, reason = "failed", "executor_error"
        else:
            if accepted == 1:
                status, reason = "cancelled", "cancel_requested"
            elif accepted == 2 and status != "cancelled":
                status, reason = "incomplete", "deadline"
        journal.emit("terminal", attempt_status=status, stop_reason=reason, handoff_reason=handoff_reason)
        cleanup = self._cleanup()
        try:
            snapshot = gate.snapshot()
            # The terminal transition now owns its gate number: assess once more with it counted.
            constraints.update(assess_constraints(self.view, snapshot, self.attempt_id,
                                                  slot_evidence=self.slot_evidence, coverage=gate.slot_snapshot(),
                                                  ownership=None, dispatches=journal.actions, revocations=[],
                                                  driver_outcomes=journal.outcomes, effect_snapshot=None))
        except RuntimeStop:
            pass
        limits = task.limits
        steps, calls = snapshot["steps"], snapshot["model_calls"]
        actions = journal.actions
        unsettled = any(a["effect"] == "unknown" for a in actions) or bool(snapshot["in_flight"])
        outcome = ("fail" if constraints.has_fail() or (status == "failed" and reason in
                                                       ("postcondition_failed", "constraint_violated"))
                   else "unknown")
        result = {
            "schema_version": RESULT_SCHEMA, "task_id": task.task_id, "revision": task.revision,
            "chain_id": standalone_chain(self.attempt_id)["root_attempt_id"], "attempt_id": self.attempt_id,
            "parent_attempt_id": None, "attempt_status": status, "stop_reason": reason, "task_outcome": outcome,
            "execution_outcome": ("unknown" if unsettled else "returned" if snapshot["dispatches"]
                                  else "error" if status == "failed" else "not_started"),
            "outputs": [], "verification": criteria + constraints.rows(),
            "actions": actions, "budget": {
                "steps": steps, "model_calls": calls, "dispatches": snapshot["dispatches"],
                "limits": {"wall_seconds": limits.wall_seconds, "max_steps": limits.max_steps,
                           "max_model_calls": limits.max_model_calls, "max_handoffs": limits.max_handoffs},
                "remaining_steps": max(0, limits.max_steps - steps),
                "remaining_model_calls": max(0, limits.max_model_calls - calls),
                "remaining_wall_seconds": round(gate.wall_remaining(), 3),
                "model_call_limit_scope": "locally_controlled_provider_requests",
                "elapsed_seconds": round(terminal_at - started, 3)},
            "handoff": self._handoff(handoff_reason, reason) if status == "incomplete" else None,
            "components": self._components(), "cleanup": cleanup, "watchdog": notes,
            "decision": {"kind": self.action_provider, "requested_model": self.requested_model,
                         "served_models": list(self.provider.served_models), "config_id": self.config_id},
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
        self.slot_evidence.close()
        journal.close()
        if self.out_dir is not None:
            (self.out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                                                      encoding="utf-8")
        return result

    def _handoff(self, handoff_reason: str | None, stop_reason: str) -> dict | None:
        if handoff_reason is None and stop_reason not in ("effect_unknown", "verification_unknown"):
            return None
        last = self.journal.last_observation
        return {
            "schema_version": "browser-handoff/0.1-partial",
            "reason": handoff_reason or stop_reason, "task_id": self.task.task_id, "revision": self.task.revision,
            "attempt_id": self.attempt_id, "observation_ref": (last or {}).get("observation_id"),
            "page": (last or {}).get("page"),
            "operations": [{"id": a["id"], "operation": a["operation"], "option_ref": a["option_ref"],
                            "slot_ref": a["slot_ref"], "driver_outcome": a["driver_outcome"], "effect": a["effect"]}
                           for a in self.journal.actions],
            "permissions": sorted(self.task.permissions), "text_slot_refs": list(self.task.slot_references),
            # A continuation token needs the session service of M3; none is issued here.
            "continuation": {"allowed": False, "reason": "resume_not_implemented"}}

    def _cleanup(self) -> dict:
        adapter = self.adapter
        if adapter is None:
            return {"status": "not_created"}
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
