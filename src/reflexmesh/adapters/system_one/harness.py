"""Optional pinned SystemOneHarness integration, independent of a browser backend."""

from __future__ import annotations

import math

from reflexmesh.runtime.runner import ControlledEnvironment, RuntimeStop, WorkerResult


class CountingProvider:
    def __init__(self, inner, gate, events, *, model: bool, faults=None):
        self.inner, self.gate, self.events, self.model = inner, gate, events, model
        self.faults = faults

    def decide(self, state, questions):
        self.gate.remaining()
        if self.model:
            self.gate.reserve_call()
        self.events.put(("decision_request", "model" if self.model else "script"))
        if self.faults is not None:
            self.faults.before_decision()
        if self.model and hasattr(self.inner, "_client"):
            self.inner._client.timeout = max(0.01, self.gate.operation_timeout(30.0))
        result = self.inner.decide(state, questions)
        self.gate.remaining()
        return result

    def close(self):
        if hasattr(self.inner, "close"):
            self.inner.close()


class HarnessStrategy:
    """Inject an environment, provider, action space, and adapter admission check."""

    def __init__(self, goal, environment_factory, space_factory, provider_factory, admit,
                 *, model=False, bootstrap=False, faults=None):
        self.goal = goal
        self.environment_factory = environment_factory
        self.space_factory = space_factory
        self.provider_factory = provider_factory
        self.admit = admit
        self.model = model
        self.bootstrap = bootstrap
        self.faults = faults

    def __call__(self, gate, events):
        from systemone_harness.controller import Controller
        from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser

        inner = self.environment_factory()
        controlled = provider = None
        try:
            if type(inner) is FixtureBrowser:
                inner.bind_ownership(gate.fixture_owner)
                inner.bind_runtime(gate.slot_registry, gate.slot_sink)
            controlled = ControlledEnvironment(inner, gate, events, self.admit or inner.admit,
                                               bootstrap=self.bootstrap, faults=self.faults)
            provider = CountingProvider(self.provider_factory(), gate, events, model=self.model,
                                        faults=self.faults)
            run = Controller(self.space_factory(), controlled, provider,
                             # The pinned controller compares against max_steps and
                             # does not accept None. Infinity is confined to this
                             # comparison; it is never sent to I/O or serialized.
                             max_steps=math.inf if gate.max_steps is None else gate.max_steps,
                             timeout_seconds=None,  # AttemptGate owns elapsed task time.
                             on_step=lambda step: events.put(("step", step.index, step.action,
                                                              step.verdict))).run(self.goal)
            if controlled.last_stop:
                raise RuntimeStop(controlled.last_stop)
            if run.status == "failed":
                return WorkerResult("protocol_error" if run.reason == "provider_error" else "executor_error",
                                    run.reason)
            if run.reason == "no_confident_action":
                controlled.verification_observation()
                return WorkerResult("no_confident_action", run.reason)
            if run.status == "completed":
                controlled.verification_observation()
                return WorkerResult("finish", run.reason)
            return WorkerResult("executor_error", run.reason)
        finally:
            try:
                if provider is not None:
                    provider.close()
            finally:
                if controlled is not None:
                    controlled.close()
                else:
                    inner.close()
