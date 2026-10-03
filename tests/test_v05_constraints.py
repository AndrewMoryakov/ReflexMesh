"""Runtime constraints, policy revision, gate failure recovery, cleanup and reconciliation (U)."""

import multiprocessing as mp
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.runner import (GRACE_SECONDS, KILL_SECONDS, AttemptSupervisor, ControlledEnvironment,
                                       WorkerResult)
from test_v05_runtime import pass_verifier, sample

BOUND = GRACE_SECONDS + KILL_SECONDS + 1.0


def task(wall=2.0, permissions=("navigate", "type_text", "submit_form")):
    data = sample()
    data["limits"]["wall_seconds"] = wall
    data["permissions"] = list(permissions)
    return ExecutionTask.from_dict(data)


class Ok:
    ok = True


class SubmitEnvironment:
    """Fake adapter: one submit target with explicit operation identity and a session."""

    def __init__(self, hang=False):
        self.hang = hang

    def reset(self, goal):
        pass

    def session_info(self):
        return {"session_id": "s-1", "fresh_profile": True, "attached": False}

    def observe(self):
        return SimpleNamespace(fields={}, candidates={})

    def describe(self, action, params):
        return {"operation": "submit_form", "target_id": "send-form"}

    def execute(self, action, params):
        if self.hang:
            time.sleep(100)
        return Ok()

    def close(self):
        pass


class EffectsVerifier:
    """Verifier with an authoritative server effect record, like FixtureVerifier.effects."""

    def __init__(self, status, requests):
        self.status, self.requests = status, requests

    def __call__(self, task, timeout):
        return [{"id": c.id, "status": self.status, "evidence_refs": ["fake://server"]} for c in task.criteria]

    def effects(self, task, timeout):
        return {"ref": "fake://server", "submit_form": {"requests": self.requests, "running": False}}


def submit_once(gate, events, hang=False):
    env = ControlledEnvironment(SubmitEnvironment(hang), gate, events, lambda *_: None)
    env.reset("goal")
    env.observe()
    env.execute("click", {"element": "1"})
    return WorkerResult("finish")


def dies_holding_gate(gate, events):
    gate.reserve_step()
    gate.commit()            # Committed, but the dispatch event never reaches the supervisor.
    gate.lock.acquire()
    os._exit(3)


def hangs_holding_gate(gate, events):
    gate.reserve_step()
    gate.lock.acquire()
    time.sleep(100)


def leaves_descendant(gate, events, pid):
    child = subprocess.Popen(["sleep", "100"])  # Inherits the worker's process group, like a browser.
    pid.value = child.pid
    gate.reserve_step()
    return WorkerResult("finish")


def never_returns(gate, events):
    gate.reserve_step()
    time.sleep(100)


def bypasses_permission(gate, events):
    # Fault injection: an executor that commits an operation outside the granted permissions.
    gate.reserve_step()
    action_id, seq = gate.commit()
    events.put(("proposal", {"action": "click"}))
    events.put(("dispatch", action_id, {"operation": "delete_account", "target_id": "delete-account",
                                        "gate_seq": seq}))
    time.sleep(1)
    return WorkerResult("finish")


def observe_wait_execute(gate, events, ready, release, twice=False):
    env = ControlledEnvironment(SubmitEnvironment(), gate, events, lambda *_: None)
    env.reset("goal")
    env.observe()
    ready.set()
    release.wait(2)
    result = env.execute("click", {"element": "1"})
    if twice and not result.ok:
        env.observe()
        env.execute("click", {"element": "1"})
    return WorkerResult("finish")


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


class ConstraintRows(unittest.TestCase):
    def test_completed_attempt_carries_every_runtime_constraint(self):
        result = AttemptSupervisor(task(), submit_once, EffectsVerifier("pass", 1)).run()
        self.assertEqual((result["attempt_status"], result["task_outcome"]), ("completed", "pass"), result)
        rows = {row["id"]: row["status"] for row in result["verification"] if row["kind"] == "execution_constraint"}
        self.assertEqual(rows, {f"runtime.{name}": "pass" for name in
                                ("permissions", "text_slots", "session", "dispatch_order",
                                 "no_uncertain_repeat", "budget")})
        self.assertEqual(result["actions"][0]["effect"], "applied")

    def test_missing_session_evidence_prevents_completion(self):
        def submit_without_session(gate, events):
            inner = SubmitEnvironment()
            inner.session_info = lambda: None
            env = ControlledEnvironment(inner, gate, events, lambda *_: None)
            env.observe()
            env.execute("click", {"element": "1"})
            return WorkerResult("finish")

        result = AttemptSupervisor(task(), submit_without_session, EffectsVerifier("pass", 1)).run()
        session = next(r for r in result["verification"] if r["id"] == "runtime.session")
        self.assertEqual(session["status"], "unknown")
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "verification_unknown"))

    def test_permission_violation_is_confirmed_and_sticky(self):
        result = AttemptSupervisor(task(), bypasses_permission, pass_verifier).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("failed", "constraint_violated", "fail"))
        permissions = next(r for r in result["verification"] if r["id"] == "runtime.permissions")
        self.assertEqual(permissions["status"], "fail")


class PolicyRevision(unittest.TestCase):
    def run_with_revocation(self, operation, twice=False):
        ctx = mp.get_context("fork")
        ready, release = ctx.Event(), ctx.Event()
        sup = AttemptSupervisor(task(), lambda g, e: observe_wait_execute(g, e, ready, release, twice),
                                EffectsVerifier("pass", 1))

        def revoke():
            self.assertTrue(ready.wait(2))
            self.assertTrue(sup.revoke(operation))
            release.set()

        thread = threading.Thread(target=revoke)
        thread.start()
        result = sup.run()
        thread.join(2)
        return result

    def test_revocation_after_selection_blocks_dispatch(self):
        result = self.run_with_revocation("submit_form")
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("blocked", "policy_denied"))
        self.assertEqual(result["budget"]["dispatches"], 0)
        self.assertEqual(result["policy"]["revision"], 2)
        self.assertEqual(result["policy"]["revocations"][0]["operation"], "submit_form")

    def test_unrelated_revocation_forces_fresh_observation(self):
        result = self.run_with_revocation("type_text", twice=True)
        self.assertIn("stale_policy", [str(row["data"]) and row["data"][0] for row in result["trace"]
                                       if row["kind"] == "reobserve"])
        self.assertEqual(result["budget"]["dispatches"], 1)
        self.assertEqual(result["attempt_status"], "completed", result)


class GateFailure(unittest.TestCase):
    def test_worker_dies_holding_gate(self):
        started = time.monotonic()
        result = AttemptSupervisor(task(), dies_holding_gate, pass_verifier).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("failed", "executor_error"))
        self.assertEqual((len(result["actions"]), result["actions"][0]["effect"]), (1, "unknown"))
        self.assertEqual(result["execution_outcome"], "unknown")
        self.assertLess(time.monotonic() - started, 4)

    def test_worker_hangs_holding_gate(self):
        result = AttemptSupervisor(task(wall=0.3), hangs_holding_gate, pass_verifier).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["cleanup"]),
                         ("failed", "executor_error", "forced"))
        self.assertLessEqual(result["timing"]["exit_after_terminal_seconds"], BOUND)


class Cleanup(unittest.TestCase):
    def test_surviving_descendant_is_terminated(self):
        pid = mp.get_context("fork").Value("i", 0)
        result = AttemptSupervisor(task(), lambda g, e: leaves_descendant(g, e, pid), pass_verifier).run()
        self.assertEqual(result["cleanup"], "forced")
        for _ in range(100):
            if not alive(pid.value):
                break
            time.sleep(0.02)
        self.assertFalse(alive(pid.value))

    def test_deadline_transition_and_exit_bounds(self):
        started = time.monotonic()
        result = AttemptSupervisor(task(wall=0.3), never_returns, pass_verifier).run()
        self.assertEqual((result["stop_reason"], result["cleanup"]), ("deadline", "forced"))
        self.assertLessEqual(result["timing"]["deadline_delay_seconds"], 0.25)
        self.assertLessEqual(result["timing"]["exit_after_terminal_seconds"], BOUND)
        self.assertLessEqual(time.monotonic() - started, 0.3 + 0.25 + BOUND)


class Reconciliation(unittest.TestCase):
    def test_lost_return_reconciled_as_applied_from_server_record(self):
        result = AttemptSupervisor(task(wall=0.5), lambda g, e: submit_once(g, e, hang=True),
                                   EffectsVerifier("unknown", 1)).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "deadline"))
        submit = result["actions"][-1]
        self.assertEqual((submit["operation"], submit["return_seq"], submit["effect"]),
                         ("submit_form", None, "applied"))

    def test_absent_request_after_browser_stop_is_not_applied(self):
        result = AttemptSupervisor(task(wall=0.5), lambda g, e: submit_once(g, e, hang=True),
                                   EffectsVerifier("unknown", 0)).run()
        self.assertEqual(result["actions"][-1]["effect"], "not_applied")
        self.assertNotEqual(result["execution_outcome"], "unknown")


if __name__ == "__main__":
    unittest.main()
