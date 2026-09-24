"""V0.4 pre-spike: probe ADR-0002 control properties of SystemOneHarness (no model, no key).

Uses the harness's own ScriptProvider and OrderWorkflow environment. For each property it records
what the unmodified controller does and whether an in-process wrapper around Environment.execute
(the only hook the controller offers before an action) can enforce it. Prints a JSON report.

Run with the SystemOneHarness venv:  <soh>/.venv/bin/python probe_control_points.py
"""
import json
import threading
import time

from systemone_harness.controller import Controller
from systemone_harness.envs.order_workflow import OrderWorkflow
from systemone_harness.environment import Environment, Result
from systemone_harness.provider import ScriptProvider

SCRIPT = ["pick_item(item='scarf')", "pack()", "choose_carrier(carrier='express')", "ship()"]
GOAL = "Ship order B-220 by the fastest carrier."


class Recording(Environment):
    """Pass-through wrapper: records every call that reaches the environment ("driver")."""

    def __init__(self, inner, veto=None, before=None):
        self.inner, self.veto, self.before, self.calls = inner, veto, before, []

    def reset(self, goal):
        self.inner.reset(goal)

    def observe(self):
        return self.inner.observe()

    def execute(self, action, params):
        if self.before:
            self.before(action)
        if self.veto and self.veto(action):
            return self.veto_result(action)
        self.calls.append(action)
        return self.inner.execute(action, params)

    veto_result = None


def run(env, provider=None, on_step=None, cancel_during=None):
    space = OrderWorkflow.action_space()
    ctl = Controller(space, env, provider or ScriptProvider(SCRIPT), max_steps=10, on_step=on_step)
    if cancel_during:
        cancel_during(ctl)
    r = ctl.run(GOAL)
    return ctl, r


def probe():
    out = {}

    # P0 baseline
    env = Recording(OrderWorkflow("ship_fastest_gift"))
    _, r = run(env)
    out["baseline"] = {"status": r.status, "reason": r.reason, "driver_calls": env.calls}

    # P1 pre-action veto. The controller has no approval hook: on_step fires after execute.
    seen = []
    env = Recording(OrderWorkflow("ship_fastest_gift"))
    run(env, on_step=lambda s: seen.append((s.action, env.calls[-1] if env.calls else None)))
    on_step_after_execute = all(a == c for a, c in seen if a not in ("finish", "escalate"))
    # Wrapper veto variants for the destructive 'ship':
    variants = {}
    for name, factory in {
        "result_not_ok": lambda a: Result(ok=False, text=f"vetoed: {a}"),
        "result_terminal": lambda a: Result(ok=False, text=f"vetoed: {a}", terminal=True),
    }.items():
        env = Recording(OrderWorkflow("ship_fastest_gift"), veto=lambda a: a == "ship")
        env.veto_result = staticmethod(factory)
        _, r = run(env)
        variants[name] = {"status": r.status, "reason": r.reason, "driver_calls": env.calls,
                          "ship_reached_driver": "ship" in env.calls}

    class Veto(Exception):
        pass

    def raise_veto(a):
        if a == "ship":
            raise Veto("vetoed by runtime")

    env = Recording(OrderWorkflow("ship_fastest_gift"), before=raise_veto)
    _, r = run(env)
    variants["raise"] = {"status": r.status, "reason": r.reason, "driver_calls": env.calls,
                         "ship_reached_driver": "ship" in env.calls, "error": r.error}
    out["P1_pre_action_veto"] = {"native_hook": False, "on_step_fires_after_execute": on_step_after_execute,
                                 "wrapper_variants": variants}

    # P2 result available: Step.result carries Result.to_dict() in on_step and the trace.
    steps = []
    env = Recording(OrderWorkflow("ship_fastest_gift"))
    run(env, on_step=steps.append)
    out["P2_result_available"] = {"results_per_step": [bool(s.result) for s in steps],
                                  "example": steps[0].result if steps else None}

    # P3 cancel mid-decision: cancel() while the provider is deciding step 2.
    class SlowScript(ScriptProvider):
        def __init__(self, *a, hook=None, **k):
            super().__init__(*a, **k)
            self.n, self.hook = 0, hook

        def decide(self, state, questions):
            self.n += 1
            if self.hook:
                self.hook(self.n)
            return super().decide(state, questions)

    holder = {}
    prov = SlowScript(SCRIPT, hook=lambda n: holder["ctl"].cancel() if n == 2 else None)
    env = Recording(OrderWorkflow("ship_fastest_gift"))
    ctl = Controller(OrderWorkflow.action_space(), env, prov, max_steps=10)
    holder["ctl"] = ctl
    r = ctl.run(GOAL)
    out["P3_cancel_during_decision"] = {"status": r.status, "reason": r.reason, "driver_calls": env.calls,
                                        "action_after_cancel_executed": len(env.calls) >= 2}
    # Wrapper enforcement: refuse execute once a runtime cancel flag is set.
    flag = threading.Event()
    prov = SlowScript(SCRIPT, hook=lambda n: flag.set() if n == 2 else None)
    env = Recording(OrderWorkflow("ship_fastest_gift"),
                    before=lambda a: (_ for _ in ()).throw(RuntimeError("cancelled")) if flag.is_set() else None)
    ctl = Controller(OrderWorkflow.action_space(), env, prov, max_steps=10)
    r = ctl.run(GOAL)
    out["P3_wrapper_blocks_after_cancel"] = {"status": r.status, "reason": r.reason, "driver_calls": env.calls}

    # P4 late response: a decision that returns after cancel/timeout must not resume the loop.
    class Late(ScriptProvider):
        def decide(self, state, questions):
            time.sleep(0.3)
            return super().decide(state, questions)

    env = Recording(OrderWorkflow("ship_fastest_gift"))
    ctl = Controller(OrderWorkflow.action_space(), env, Late(SCRIPT), max_steps=10, timeout_seconds=0.1)
    r = ctl.run(GOAL)
    out["P4_timeout_then_late_decision"] = {"status": r.status, "reason": r.reason, "driver_calls": env.calls,
                                            "late_decision_executed": len(env.calls) > 0}
    return out


if __name__ == "__main__":
    print(json.dumps(probe(), indent=1, default=str))
