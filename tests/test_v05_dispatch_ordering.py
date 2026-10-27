"""The runtime.dispatch_ordering row: gate sequence numbers accounted for exactly once."""

import multiprocessing as mp
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.runner import AttemptGate, AttemptSupervisor, ControlledEnvironment, WorkerResult
from reflexmesh.verification.constraints import assess_constraints
from test_v05_constraints import EffectsVerifier, SubmitEnvironment
from test_v05_runtime import pass_verifier, sample


def task():
    data = sample()
    data["limits"]["wall_seconds"] = 30
    data["permissions"] = ["navigate", "type_text", "submit_form"]
    return ExecutionTask.from_dict(data)


def make_gate():
    return AttemptGate(mp.get_context("fork"), task(), time.monotonic())


def dispatch(id_, gate_seq, *, proposal=None, at=None, ret=None):
    at = id_ * 10 if at is None else at
    return {"id": id_, "operation": "submit_form", "gate_seq": gate_seq,
            "proposal_seq": at - 1 if proposal is None else proposal, "dispatch_seq": at, "return_seq": ret}


def ordering(dispatches, *, seq, terminal_seq=0, revocations=(), count=None, snapshot=True):
    snap = {"steps": 1, "model_calls": 0, "dispatches": len(dispatches) if count is None else count,
            "committed_operations": 0, "unchecked_commits": 0, "seq": seq, "terminal_seq": terminal_seq}
    rows = assess_constraints(task(), snap if snapshot else None, "attempt-1",
                              dispatches=list(dispatches), revocations=list(revocations))
    return next(r for r in rows if r["id"] == "runtime.dispatch_ordering")


class GateSequence(unittest.TestCase):
    def test_commits_revocations_and_the_terminal_transition_share_one_sequence(self):
        gate = make_gate()
        gate.reserve_step()
        first = gate.commit_dispatch_seq(operation="submit_form")
        gate.settle(first[0])
        revoked = gate.revoke("type_text")
        gate.stop("terminal")
        snap = gate.snapshot()
        self.assertEqual((first[1], revoked, snap["terminal_seq"], snap["seq"]), (1, 2, 3, 3))

    def test_only_the_first_terminal_transition_takes_a_number(self):
        gate = make_gate()
        self.assertTrue(gate.stop("cancel_requested"))
        self.assertFalse(gate.stop("terminal"))
        gate.finish()
        snap = gate.snapshot()
        self.assertEqual((snap["terminal_seq"], snap["seq"], snap["terminal"]), (1, 1, 1))

    def test_a_refused_revocation_takes_no_number(self):
        gate = make_gate()
        self.assertEqual(gate.revoke("click"), 0)
        gate.stop("terminal")
        self.assertEqual(gate.revoke("type_text"), 0)
        self.assertEqual(gate.snapshot()["seq"], 1)

    def test_rejected_commits_take_no_number(self):
        gate = make_gate()
        gate.reserve_step()
        gate.revoke("submit_form")
        before = gate.snapshot()["seq"]
        with self.assertRaises(Exception):
            gate.commit_dispatch(operation="submit_form")
        self.assertEqual(gate.snapshot()["seq"], before)


class OrderingRow(unittest.TestCase):
    def test_pass_when_every_number_is_accounted_for_once(self):
        result = ordering([dispatch(1, 1, ret=15), dispatch(2, 3, at=30)], seq=4, terminal_seq=4,
                          revocations=[{"operation": "type_text", "gate_seq": 2}])
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["evidence_refs"] and result["reason"])

    def test_no_dispatches_and_nothing_issued_is_a_pass(self):
        self.assertEqual(ordering([], seq=0)["status"], "pass")

    def test_missing_snapshot_is_unknown(self):
        self.assertEqual(ordering([dispatch(1, 1)], seq=1, snapshot=False)["status"], "unknown")

    def test_dispatch_lists_must_exist(self):
        rows = assess_constraints(task(), {"steps": 0, "model_calls": 0, "dispatches": 0, "seq": 0,
                                           "terminal_seq": 0}, "attempt-1")
        self.assertEqual(next(r for r in rows if r["id"] == "runtime.dispatch_ordering")["status"], "unknown")

    def test_an_unaccounted_gate_number_is_unknown_not_pass(self):
        # The gate issued 3 numbers but only two are explained: a revocation event was lost.
        self.assertEqual(ordering([dispatch(1, 1), dispatch(2, 3, at=30)], seq=3)["status"], "unknown")

    def test_a_duplicated_number_is_unknown(self):
        result = ordering([dispatch(1, 1), dispatch(2, 2, at=30)], seq=2,
                          revocations=[{"operation": "type_text", "gate_seq": 2}])
        self.assertEqual(result["status"], "unknown")

    def test_a_non_integer_revocation_number_is_unknown(self):
        result = ordering([dispatch(1, 1)], seq=2, revocations=[{"operation": "type_text", "gate_seq": "2"}])
        self.assertEqual(result["status"], "unknown")

    def test_fewer_reported_dispatches_than_committed_is_unknown(self):
        self.assertEqual(ordering([dispatch(1, 1)], seq=2, count=2)["status"], "unknown")

    def test_a_snapshot_that_disagrees_with_itself_is_unknown(self):
        # One number issued and one reported, yet the snapshot counts two commits.
        self.assertEqual(ordering([dispatch(1, 1)], seq=1, count=2)["status"], "unknown")

    def test_a_dispatch_without_a_gate_number_is_unknown(self):
        row = dispatch(1, 1)
        del row["gate_seq"]
        self.assertEqual(ordering([row], seq=1)["status"], "unknown")

    def test_ids_must_be_consecutive(self):
        self.assertEqual(ordering([dispatch(1, 1), dispatch(3, 2, at=30)], seq=2)["status"], "fail")

    def test_gate_numbers_must_increase_with_the_action_id(self):
        self.assertEqual(ordering([dispatch(1, 2), dispatch(2, 1, at=30)], seq=2)["status"], "fail")

    def test_a_dispatch_needs_an_earlier_proposal(self):
        self.assertEqual(ordering([dispatch(1, 1, proposal=10, at=10)], seq=1)["status"], "fail")
        row = dispatch(1, 1)
        row["proposal_seq"] = None
        self.assertEqual(ordering([row], seq=1)["status"], "fail")

    def test_a_return_must_follow_its_dispatch(self):
        self.assertEqual(ordering([dispatch(1, 1, at=10, ret=10)], seq=1)["status"], "fail")
        self.assertEqual(ordering([dispatch(1, 1, at=10, ret=9)], seq=1)["status"], "fail")

    def test_a_number_after_the_terminal_transition_is_a_confirmed_failure(self):
        result = ordering([dispatch(1, 3)], seq=3, terminal_seq=2)
        self.assertEqual(result["status"], "fail")

    def test_a_number_the_gate_never_issued_is_a_confirmed_failure(self):
        self.assertEqual(ordering([dispatch(1, 9)], seq=1)["status"], "fail")


def submit_once(gate, events):
    env = ControlledEnvironment(SubmitEnvironment(), gate, events, lambda *_: None)
    env.reset("goal")
    env.observe()
    env.execute("click", {"element": "1"})
    return WorkerResult("finish")


def commit_without_reporting(gate, events):
    gate.reserve_step()
    gate.commit_dispatch(operation="submit_form")
    return WorkerResult("finish")


def reports_a_moved_number(gate, events):
    gate.reserve_step()
    action_id, gate_seq = gate.commit_dispatch_seq(operation="submit_form")
    events.put(("proposal", {"action": "click"}))
    events.put(("dispatch", action_id, {"operation": "submit_form", "target_id": "send-form",
                                        "gate_seq": gate_seq + 5}))
    return WorkerResult("finish")


def row_of(result, name):
    return next(r for r in result["verification"] if r["id"] == f"runtime.{name}")


class EndToEnd(unittest.TestCase):
    def test_a_real_controlled_dispatch_passes_the_ordering_row(self):
        result = AttemptSupervisor(task(), submit_once, EffectsVerifier("pass", 1)).run()
        self.assertEqual(row_of(result, "dispatch_ordering")["status"], "pass", result["verification"])
        self.assertEqual(result["actions"][0]["gate_seq"], 1)

    def test_a_commit_the_worker_never_reported_is_unknown(self):
        result = AttemptSupervisor(task(), commit_without_reporting, pass_verifier).run()
        self.assertEqual(row_of(result, "dispatch_ordering")["status"], "unknown")

    def test_a_worker_that_moves_its_gate_number_fails_the_attempt(self):
        result = AttemptSupervisor(task(), reports_a_moved_number, pass_verifier).run()
        self.assertEqual(row_of(result, "dispatch_ordering")["status"], "fail")
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("failed", "constraint_violated"))


if __name__ == "__main__":
    unittest.main()
