"""A worker that dies or hangs holding the gate lock cannot stall or inflate the attempt."""

import multiprocessing as mp
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.runtime.runner import AttemptGate, AttemptSupervisor, RuntimeStop
from test_v05_constraints import BOUND, dies_holding_gate, hangs_holding_gate, task
from test_v05_runtime import pass_verifier

FAST = 1.0  # One gate acquisition waits 0.25 s; anything near a second means it blocked.


def constraint(result, name):
    return next(r for r in result["verification"] if r["id"] == f"runtime.{name}")


class ForeignLock(unittest.TestCase):
    def setUp(self):
        ctx = mp.get_context("fork")
        self.gate = AttemptGate(ctx, task(), time.monotonic())
        held = ctx.Event()

        def hold(gate, held):
            gate.lock.acquire()
            held.set()
            time.sleep(60)

        self.child = ctx.Process(target=hold, args=(self.gate, held))
        self.child.start()
        self.assertTrue(held.wait(5))

    def tearDown(self):
        self.child.kill()
        self.child.join(2)

    def test_every_gate_operation_fails_fast_instead_of_waiting_for_the_holder(self):
        operations = {
            "reserve_step": self.gate.reserve_step,
            "reserve_call": self.gate.reserve_call,
            "snapshot": self.gate.snapshot,
            "slot_snapshot": self.gate.slot_snapshot,
            "seal_dispatches": self.gate.seal_dispatches,
            "revision": self.gate.revision,
            "permitted": lambda: self.gate.permitted("navigate"),
            "revoke": lambda: self.gate.revoke("navigate"),
            "commit_dispatch": lambda: self.gate.commit_dispatch(operation="navigate"),
            "settle": lambda: self.gate.settle(1),
            "stop": lambda: self.gate.stop("cancel_requested"),
            "finish": self.gate.finish,
            "remaining": self.gate.remaining,
        }
        for name, call in operations.items():
            started = time.monotonic()
            with self.assertRaises(RuntimeStop, msg=name) as caught:
                call()
            self.assertEqual(caught.exception.reason, "gate_unavailable", name)
            self.assertLess(time.monotonic() - started, FAST, name)


class DeadHolder(unittest.TestCase):
    def test_no_constraint_passes_without_a_synchronized_gate(self):
        result = AttemptSupervisor(task(), dies_holding_gate, pass_verifier).run()
        for name in ("permissions", "dispatch_ordering", "budget"):
            self.assertEqual(constraint(result, name)["status"], "unknown", name)
        self.assertEqual(result["task_outcome"], "unknown")
        self.assertNotEqual(result["attempt_status"], "completed")

    def test_the_committed_dispatch_is_kept_as_an_unknown_effect(self):
        result = AttemptSupervisor(task(), dies_holding_gate, pass_verifier).run()
        self.assertEqual([(a["id"], a["effect"]) for a in result["actions"]], [(1, "unknown")])
        self.assertEqual(result["execution_outcome"], "unknown")

    def test_the_attempt_returns_within_the_exit_bound(self):
        started = time.monotonic()
        result = AttemptSupervisor(task(), dies_holding_gate, pass_verifier).run()
        self.assertLess(time.monotonic() - started, 4)
        self.assertLessEqual(result["timing"]["exit_after_terminal_seconds"], BOUND)


class HungHolder(unittest.TestCase):
    def test_a_hung_holder_is_killed_and_the_attempt_ends_inside_the_bound(self):
        started = time.monotonic()
        result = AttemptSupervisor(task(wall=0.3), hangs_holding_gate, pass_verifier).run()
        self.assertEqual(result["worker_cleanup"], "forced")
        self.assertLessEqual(result["timing"]["exit_after_terminal_seconds"], BOUND)
        self.assertLessEqual(time.monotonic() - started, 0.3 + 0.25 + BOUND)

    def test_nothing_is_certified_when_the_gate_cannot_be_read(self):
        result = AttemptSupervisor(task(wall=0.3), hangs_holding_gate, pass_verifier).run()
        self.assertEqual(result["cleanup"], "unknown")
        self.assertEqual(result["task_outcome"], "unknown")
        self.assertEqual(constraint(result, "dispatch_ordering")["status"], "unknown")


class SupervisorControls(unittest.TestCase):
    def test_cancel_and_revoke_do_not_block_on_a_foreign_lock(self):
        supervisor = AttemptSupervisor(task(), dies_holding_gate, pass_verifier)
        ctx = mp.get_context("fork")
        held = ctx.Event()

        def hold(gate, held):
            gate.lock.acquire()
            held.set()
            time.sleep(60)

        child = ctx.Process(target=hold, args=(supervisor.gate, held))
        child.start()
        try:
            self.assertTrue(held.wait(5))
            started = time.monotonic()
            self.assertFalse(supervisor.cancel())
            self.assertFalse(supervisor.revoke("submit_form"))
            self.assertLess(time.monotonic() - started, 2 * FAST)
            self.assertEqual(supervisor.revocations, [])
        finally:
            child.kill()
            child.join(2)
            supervisor.slot_evidence.close()


if __name__ == "__main__":
    unittest.main()
