"""Optional pinned SystemOneHarness integration, independent of a browser backend."""

from __future__ import annotations

import time

from reflexmesh.runtime.faults import request_cancel, sleep_forever
from reflexmesh.runtime.runner import ControlledEnvironment, RuntimeStop, WorkerResult


class CountingProvider:
    def __init__(self, inner, gate, events, *, model: bool, faults: dict | None = None):
        self.inner, self.gate, self.events, self.model = inner, gate, events, model
        self.faults = dict(faults or {})
        self.decisions = 0

    def decide(self, state, questions):
        if self.model:
            self.gate.reserve_call()
            if hasattr(self.inner, "_client"):
                self.inner._client.timeout = max(0.01, min(30.0, self.gate.remaining()))
        self.decisions += 1
        self.events.put(("decision_request", "model" if self.model else "script"))
        if self.faults.get("hang_provider_at") == self.decisions:
            sleep_forever()
        if self.faults.get("provider_error_at") == self.decisions:
            from systemone_harness.provider import ProviderError
            raise ProviderError("injected provider protocol error")
        if self.faults.get("cancel_on_decision") == self.decisions:
            request_cancel(self.gate)  # The decision below is pending when cancellation lands.
        decision = self.inner.decide(state, questions)
        hold = self.faults.get("hold_decision")
        if hold and hold["at"] == self.decisions:
            # Barrier for external cancellation: announce the pending decision in the working directory.
            try:
                with open("fault-hold-started", "w") as marker:
                    marker.write(str(self.decisions))
            except OSError:
                pass
            time.sleep(hold["seconds"])
        if self.model:
            # Usage only: never the state, questions or answers.
            raw_usage = (getattr(decision, "raw", None) or {}).get("usage") or {}
            cost = raw_usage.get("cost")
            usage = getattr(decision, "usage", None) or {}
            self.events.put(("decision", {
                "served_model": str(getattr(decision, "model", "") or ""),
                "request_id": str(getattr(decision, "request_id", "") or ""),
                "latency_ms": getattr(decision, "latency_ms", None),
                "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                "cost": cost if type(cost) in (int, float) else None}))
        return decision

    def close(self):
        if hasattr(self.inner, "close"):
            self.inner.close()


class HarnessStrategy:
    """Inject an environment, provider, action space, and adapter admission check."""

    def __init__(self, goal, environment_factory, space_factory, provider_factory, admit,
                 *, model=False, bootstrap=False, faults: dict | None = None):
        self.goal = goal
        self.environment_factory = environment_factory
        self.space_factory = space_factory
        self.provider_factory = provider_factory
        self.admit = admit
        self.model = model
        self.bootstrap = bootstrap
        self.faults = dict(faults or {})

    def _hooks(self, gate, events):
        faults = self.faults

        def hit(key, value):
            # A fault fires only when configured and the descriptor names that value.
            return key in faults and value is not None and faults[key] == value

        def before_commit(descriptor):
            target, operation = descriptor.get("target_id"), descriptor.get("operation")
            if hit("revoke_on_commit", operation):
                # An external revocation lands after selection and revalidation (C13).
                seq = gate.revoke(operation)
                if seq:
                    events.put(("revoked", operation, seq))
            if hit("cancel_before_commit", target):
                request_cancel(gate)

        def after_commit(descriptor):
            if hit("cancel_after_commit", descriptor.get("target_id")):
                if request_cancel(gate) and faults.get("corrupt_budget_after_cancel"):
                    # A confirmed constraint violation that becomes visible after cancellation.
                    gate.steps.value = gate.max_steps + 1

        return before_commit, after_commit

    def __call__(self, gate, events):
        from systemone_harness.controller import Controller

        inner = self.environment_factory()
        if hasattr(inner, "gate"):
            inner.gate = gate
        before_commit, after_commit = self._hooks(gate, events)
        controlled = ControlledEnvironment(inner, gate, events, self.admit or inner.admit,
                                           bootstrap=self.bootstrap,
                                           before_commit=before_commit, after_commit=after_commit)
        provider_inner = self.provider_factory()
        if hasattr(provider_inner, "bind"):
            provider_inner.bind(inner)
        provider = CountingProvider(provider_inner, gate, events, model=self.model, faults=self.faults)
        try:
            run = Controller(self.space_factory(), controlled, provider,
                             max_steps=gate.max_steps,
                             on_step=lambda step: events.put(("step", step.index, step.action,
                                                              step.verdict))).run(self.goal)
            if controlled.last_stop:
                raise RuntimeStop(controlled.last_stop)
            if run.status == "failed":
                return WorkerResult("protocol_error" if run.reason == "provider_error" else "executor_error",
                                    run.reason)
            if run.reason in ("no_confident_action", "escalation_requested"):
                # The micro loop handed control back; verify what is there and return a handoff.
                controlled.verification_observation()
                return WorkerResult("no_confident_action", run.reason)
            if run.status == "completed":
                controlled.verification_observation()
                return WorkerResult("finish", run.reason)
            return WorkerResult("executor_error", run.reason)
        finally:
            provider.close()
            controlled.close()
