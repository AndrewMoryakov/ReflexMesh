"""Handoff and continuation in the same browser session (V0.6 section 10, M3).

`BrowserCore` is the embedding surface of section 14: `execute(task)`, `resume(continuation)`, `cancel(chain_id)`.
A chain is one task across its attempts. It owns what must outlive an attempt: the shared budget (one deadline from
the chain's start, steps and model calls counted across attempts), the browser session, the effect registry (every
action of every attempt, and the repeat guard's memory), confirmed constraint failures, the claims of registered
checks, the owner epoch and the one continuation token that is outstanding.

An attempt that asks for help ends `incomplete/needs_handoff` and keeps that status forever. If its session can be
handed over (no driver-level unknown effect, nothing in flight, handoffs left), the chain keeps the browser open and
issues a one-time token with a lease (60 s by default, never past the deadline). When the lease runs out the browser
is closed and the token is dead. `resume` redeems the token and starts a new linked attempt in the same session with a
new owner epoch and a fresh observation; the old attempt's gate is terminal, so its environment cannot dispatch.

Two continuation modes:
  * `jev`: a new attempt runs the ordinary loop, optionally on a new task revision (same task, nothing wider);
  * `llm`: the caller proposes one action at a time from the fresh finite options (`BrowserProgress`), at most five
    per handoff, each through the same admission and gate; then `return_to_jev` or `stop`.

A one-shot CLI process holds no session between calls, so it never advertises resume (section 14).
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import secrets
import threading
import time
import uuid
from pathlib import Path

from reflexmesh.contracts.browser import BrowserTask
from reflexmesh.contracts.task import ValidationError

LEASE_SECONDS = 60.0
ASSIST_STEPS = 5


def _error(code: str, **detail) -> dict:
    return {"schema_version": "browser-error/0.1", "error": {"code": code, **detail}}


def accept_revision(old: BrowserTask, new: BrowserTask) -> str | None:
    """None when `new` may continue `old`'s chain (section 14), else the reason it may not."""
    if new.task_id != old.task_id:
        return "different_task"
    if new.revision <= old.revision:
        return "revision_not_newer"
    if not new.permissions <= old.permissions or not set(new.allowed_origins) <= set(old.allowed_origins):
        return "authority_widened"
    a, b = old.limits, new.limits
    if (b.wall_seconds > a.wall_seconds or b.max_steps > a.max_steps or b.max_model_calls > a.max_model_calls or
            b.max_handoffs > a.max_handoffs):
        return "budget_widened"
    before = {s.reference: s for s in old.slots}
    for slot in new.slots:
        kept = before.get(slot.reference)
        # An old reference never points at a new value: a changed value needs a new version.
        if kept is not None and (kept.value != slot.value or kept.sensitivity != slot.sensitivity):
            return "slot_reference_redirected"
    return None


class TaskChain:
    def __init__(self, task: BrowserTask, out_dir: Path | None, lease_seconds: float):
        self.chain_id = uuid.uuid4().hex
        self.session_id = uuid.uuid4().hex
        self.task = task
        self.root_limits = task.limits
        self.started = time.monotonic()
        self.out_dir = out_dir
        self.lease_seconds = lease_seconds
        self.lock = threading.RLock()
        self.used = {"steps": 0, "model_calls": 0}
        self.handoffs_used = 0
        self.owner_epoch = 0
        self.attempts: list[str] = []
        self.session = None                 # the GenericBrowserAdapter kept across a handoff
        self.carry: dict | None = None      # the repeat guard's memory (section 9: chain-scoped)
        self.actions: list[dict] = []       # every earlier attempt's action rows: the effect registry
        self.failed_constraints: dict = {}  # a confirmed constraint failure stays in the chain (section 11)
        self.claimed: list = []
        self.assist = None                  # the LLM-assisted attempt awaiting a proposal, if any
        self.runs: list = []                # every attempt object, in order (their gates stay terminal)
        self.closed = False
        self._token_hash: str | None = None
        self._token_attempt: str | None = None
        self._token_deadline: float | None = None
        self._timer: threading.Timer | None = None
        self._generation = 0                # which token a lease timer belongs to

    # ── budget ──
    def deadline(self) -> float:
        return self.started + self.root_limits.wall_seconds

    def remaining_limits(self):
        r = self.root_limits
        return dataclasses.replace(r, max_steps=max(0, r.max_steps - self.used["steps"]),
                                   max_model_calls=max(0, r.max_model_calls - self.used["model_calls"]))

    def remaining(self) -> dict:
        limits = self.remaining_limits()
        return {"steps": limits.max_steps, "model_calls": limits.max_model_calls,
                "wall_seconds": round(max(0.0, self.deadline() - time.monotonic()), 3),
                "handoffs": max(0, self.root_limits.max_handoffs - self.handoffs_used)}

    def parent(self) -> str | None:
        return self.attempts[-1] if self.attempts else None

    # ── token and lease ──
    def issue_token(self, attempt_id: str) -> tuple[str, float]:
        """One outstanding token, bound to this chain, its session, the attempt and the next owner epoch."""
        with self.lock:
            token = secrets.token_urlsafe(24)
            self._token_hash = hashlib.sha256(token.encode()).hexdigest()
            self._token_attempt = attempt_id
            lease = min(self.lease_seconds, max(0.0, self.deadline() - time.monotonic()))
            self._token_deadline = time.monotonic() + lease
            if self._timer is not None:
                self._timer.cancel()
            self._generation += 1
            # A timer that already fired and waits for the lock must not kill a token issued after it.
            self._timer = threading.Timer(lease, self.expire, args=(self._generation,))
            self._timer.daemon = True
            self._timer.start()
            return token, lease

    def redeem(self, token: object, parent_attempt_id: object, *, new_owner: bool = True) -> str | None:
        """Spend the token once; None when it was valid. A second use, a foreign chain or a late one fails."""
        with self.lock:
            if self.closed:
                return "chain_closed"
            if (type(token) is not str or self._token_hash is None or
                    not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), self._token_hash)):
                return "token_invalid"
            if parent_attempt_id != self._token_attempt:
                return "token_invalid"
            if time.monotonic() > (self._token_deadline or 0):
                return "token_expired"
            self._token_hash = self._token_attempt = None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if new_owner:
                self.owner_epoch += 1
            return None

    def expire(self, generation: int | None = None) -> None:
        """The lease ran out with nobody resuming: the session is closed, not left open for anyone."""
        with self.lock:
            if self._token_hash is None or self.closed or (generation is not None and generation != self._generation):
                return
            self._token_hash = self._token_attempt = None
            assist, self.assist = self.assist, None
        if assist is not None:
            assist.assist_abandon("lease_expired")
        self.close()

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self._token_hash = None
            if self._timer is not None:
                self._timer.cancel()
            session, self.session = self.session, None
            claimed, self.claimed = self.claimed, []
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        for check in claimed:
            try:
                check.release(timeout=1.0)
            except Exception:
                pass

    # ── after each attempt ──
    def record(self, attempt_id: str, snapshot: dict | None, actions: list[dict], constraints: list[dict]) -> None:
        with self.lock:
            self.attempts.append(attempt_id)
            if snapshot:
                self.used["steps"] += int(snapshot.get("steps") or 0)
                self.used["model_calls"] += int(snapshot.get("model_calls") or 0)
            self.actions.extend(dict(row, attempt_id=attempt_id) for row in actions)
            for row in constraints:
                if row.get("status") == "fail" and row["id"] not in self.failed_constraints:
                    self.failed_constraints[row["id"]] = dict(row)


class BrowserCore:
    """Embedding API: execute, resume, cancel. Results are `browser-result/0.1`, `browser-progress/0.1`, or
    `browser-error/0.1` when a continuation is refused (nothing ran)."""

    def __init__(self, *, chrome: str | None, provider_factory, action_provider: str, out_dir: Path | None = None,
                 sandbox: bool = True, headless: bool = True, checks: dict | None = None,
                 lease_seconds: float = LEASE_SECONDS):
        self.chrome, self.provider_factory, self.action_provider = chrome, provider_factory, action_provider
        self.out_dir = Path(out_dir) if out_dir else None
        self.sandbox, self.headless = sandbox, headless
        self.checks = dict(checks or {})
        self.lease_seconds = lease_seconds
        self.chains: dict[str, TaskChain] = {}
        self.running: dict[str, object] = {}

    def _run(self, chain: TaskChain, task: BrowserTask, provider: str):
        from reflexmesh.runtime.browser_run import BrowserRun

        seq = len(chain.attempts) + 1
        out = chain.out_dir / f"attempt-{seq}" if chain.out_dir else None
        run = BrowserRun(task, chrome=self.chrome, provider_factory=self.provider_factory, action_provider=provider,
                         out_dir=out, sandbox=self.sandbox, headless=self.headless, checks=self.checks, chain=chain)
        chain.runs.append(run)
        return run

    def execute(self, task: BrowserTask) -> dict:
        chain = TaskChain(task, self.out_dir, self.lease_seconds)
        self.chains[chain.chain_id] = chain
        return self._attempt(chain, task)

    def _attempt(self, chain: TaskChain, task: BrowserTask) -> dict:
        run = self._run(chain, task, self.action_provider)
        self.running[chain.chain_id] = run
        try:
            return run.run()
        finally:
            self.running.pop(chain.chain_id, None)

    def resume(self, continuation: dict) -> dict:
        if type(continuation) is not dict:
            return _error("invalid_continuation")
        chain = next((c for c in self.chains.values() if c.parent() == continuation.get("parent_attempt_id") or
                      (c.assist is not None and c.assist.attempt_id == continuation.get("parent_attempt_id"))), None)
        if chain is None:
            return _error("token_invalid")
        operation, mode = continuation.get("operation"), continuation.get("mode")
        if operation not in ("start", "step", "return_to_jev", "stop"):
            return _error("invalid_continuation")
        assisting = chain.assist
        if operation in ("step", "return_to_jev", "stop") and assisting is None:
            return _error("not_assisting")
        if operation == "start" and assisting is not None:
            return _error("assisting")
        task = chain.task
        if operation == "start" and continuation.get("task") is not None:
            try:
                task = BrowserTask.from_dict(continuation["task"])
            except (ValidationError, ValueError, TypeError):
                return _error("invalid_revision")
            reason = accept_revision(chain.task, task)
            if reason:
                return _error("revision_refused", reason=reason)
        if operation in ("start", "return_to_jev") and chain.handoffs_used >= chain.root_limits.max_handoffs:
            return _error("handoff_limit")
        if operation == "start" and chain.session is None:
            # Never a fresh browser in disguise: the continuation is in the same session or not at all.
            return _error("session_closed")
        if operation == "start" and mode not in ("jev", "llm"):
            return _error("invalid_continuation")
        problem = chain.redeem(continuation.get("token"), continuation.get("parent_attempt_id"),
                               new_owner=operation == "start")
        if problem:
            return _error(problem)
        if operation == "step":
            return assisting.assist_step(continuation.get("proposal"))
        if operation == "stop":
            chain.assist = None
            return assisting.assist_end("assistance_stopped")
        if operation == "return_to_jev":
            chain.assist = None
            handed = assisting.assist_end("assistance_returned")
            if handed.get("attempt_status") != "incomplete" or chain.session is None:
                return handed
            # The return is a handoff of its own (section 10): it spends one and starts a Jev attempt.
            problem = chain.redeem(handed["handoff"]["continuation"]["token"], handed["attempt_id"])
            if problem:
                return _error(problem)
            chain.handoffs_used += 1
            return self._attempt(chain, chain.task)
        # start
        chain.handoffs_used += 1
        if task is not chain.task:
            chain.task = task
        if mode == "llm":
            run = self._run(chain, task, "llm")
            chain.assist = run
            return run.start_assist()
        return self._attempt(chain, task)

    def cancel(self, chain_id: str) -> dict:
        chain = self.chains.get(chain_id)
        if chain is None:
            return {"chain_id": chain_id, "accepted": False, "reason": "unknown_chain"}
        run = self.running.get(chain_id)
        if run is not None:
            run.cancel()
        assist = chain.assist
        chain.assist = None
        if assist is not None:
            assist.assist_abandon("cancel_requested")
        chain.close()
        return {"chain_id": chain_id, "accepted": True}

    def close(self) -> None:
        for chain in list(self.chains.values()):
            chain.close()
