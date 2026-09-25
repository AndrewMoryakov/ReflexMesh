"""Optional pinned SystemOneHarness integration, independent of a browser backend."""

from __future__ import annotations

from reflexmesh.runtime.runner import ControlledEnvironment, RuntimeStop, WorkerResult


class CountingProvider:
    def __init__(self, inner, gate, events, *, model: bool):
        self.inner, self.gate, self.events, self.model = inner, gate, events, model

    def decide(self, state, questions):
        if self.model:
            self.gate.reserve_call()
            if hasattr(self.inner, "_client"):
                self.inner._client.timeout = max(0.01, min(30.0, self.gate.remaining()))
        self.events.put(("decision_request", "model" if self.model else "script"))
        return self.inner.decide(state, questions)

    def close(self):
        if hasattr(self.inner, "close"):
            self.inner.close()


class HarnessStrategy:
    """Inject an environment, provider, action space, and adapter admission check."""

    def __init__(self, goal, environment_factory, space_factory, provider_factory, admit,
                 *, model=False, bootstrap=False):
        self.goal = goal
        self.environment_factory = environment_factory
        self.space_factory = space_factory
        self.provider_factory = provider_factory
        self.admit = admit
        self.model = model
        self.bootstrap = bootstrap

    def __call__(self, gate, events):
        from systemone_harness.controller import Controller

        inner = self.environment_factory()
        controlled = ControlledEnvironment(inner, gate, events, self.admit or inner.admit,
                                           bootstrap=self.bootstrap)
        provider = CountingProvider(self.provider_factory(), gate, events, model=self.model)
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
            if run.reason == "no_confident_action":
                controlled.verification_observation()
                return WorkerResult("no_confident_action", run.reason)
            if run.status == "completed":
                controlled.verification_observation()
                return WorkerResult("finish", run.reason)
            return WorkerResult("executor_error", run.reason)
        finally:
            provider.close()
            controlled.close()
