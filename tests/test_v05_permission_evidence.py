"""The runtime.permissions row passes only on the gate's own record of checked commits."""

import multiprocessing as mp
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.contracts.execution import ExecutionTask, OPERATION_BITS
from reflexmesh.runtime.runner import AttemptGate, RuntimeStop
from reflexmesh.verification.constraints import assess_constraints
from test_v05_runtime import sample

GRANTED = ("navigate", "type_text", "submit_form")


def task():
    data = sample()
    data["limits"]["wall_seconds"] = 30
    data["permissions"] = list(GRANTED)
    return ExecutionTask.from_dict(data)


def make_gate():
    return AttemptGate(mp.get_context("fork"), task(), time.monotonic())


def row(snapshot, operations):
    dispatches = None if operations is None else [{"operation": op} for op in operations]
    rows = assess_constraints(task(), snapshot, "attempt-1", dispatches=dispatches)
    return next(r for r in rows if r["id"] == "runtime.permissions")


def snapshot(*, dispatches, committed=(), unchecked=0):
    bits = 0
    for name in committed:
        bits |= OPERATION_BITS[name]
    return {"steps": 1, "model_calls": 0, "dispatches": dispatches,
            "committed_operations": bits, "unchecked_commits": unchecked}


class GateRecord(unittest.TestCase):
    def test_checked_commit_records_its_operation(self):
        gate = make_gate()
        gate.reserve_step()
        gate.commit_dispatch(operation="submit_form")
        snap = gate.snapshot()
        self.assertEqual(snap["committed_operations"], OPERATION_BITS["submit_form"])
        self.assertEqual(snap["unchecked_commits"], 0)

    def test_commit_without_an_operation_is_counted_as_unchecked(self):
        gate = make_gate()
        gate.reserve_step()
        gate.commit_dispatch()
        snap = gate.snapshot()
        self.assertEqual((snap["committed_operations"], snap["unchecked_commits"]), (0, 1))

    def test_unknown_operation_is_unchecked_not_silently_accepted(self):
        gate = make_gate()
        gate.reserve_step()
        gate.commit_dispatch(operation="click")
        self.assertEqual(gate.snapshot()["unchecked_commits"], 1)

    def test_revoked_operation_cannot_commit_and_leaves_no_record(self):
        gate = make_gate()
        gate.reserve_step()
        self.assertTrue(gate.revoke("submit_form"))
        with self.assertRaises(RuntimeStop) as caught:
            gate.commit_dispatch(operation="submit_form")
        self.assertEqual(caught.exception.reason, "policy_denied")
        snap = gate.snapshot()
        self.assertEqual((snap["dispatches"], snap["committed_operations"], snap["unchecked_commits"]), (0, 0, 0))

    def test_ungranted_operation_cannot_commit(self):
        gate = make_gate()
        gate.reserve_step()
        with self.assertRaises(RuntimeStop) as caught:
            gate.commit_dispatch(operation="delete_account")
        self.assertEqual(caught.exception.reason, "policy_denied")

    def test_stale_revision_is_not_recorded_as_a_commit(self):
        gate = make_gate()
        gate.reserve_step()
        observed = gate.revision()
        gate.revoke("type_text")
        with self.assertRaises(RuntimeStop) as caught:
            gate.commit_dispatch(operation="submit_form", revision=observed)
        self.assertEqual(caught.exception.reason, "stale_policy")
        self.assertEqual(gate.snapshot()["dispatches"], 0)


class PermissionsRow(unittest.TestCase):
    def test_pass_needs_the_gate_record(self):
        result = row(snapshot(dispatches=2, committed=("navigate", "submit_form")), ["navigate", "submit_form"])
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["evidence_refs"] and result["reason"])

    def test_no_dispatches_is_a_pass_from_a_valid_gate_snapshot(self):
        self.assertEqual(row(snapshot(dispatches=0), [])["status"], "pass")

    def test_worker_events_alone_never_pass(self):
        self.assertEqual(row(None, ["submit_form"])["status"], "unknown")
        self.assertEqual(row({"steps": 1, "model_calls": 0}, ["submit_form"])["status"], "unknown")

    def test_an_unchecked_commit_blocks_the_pass_even_if_events_look_fine(self):
        result = row(snapshot(dispatches=1, committed=(), unchecked=1), ["submit_form"])
        self.assertEqual(result["status"], "unknown")

    def test_one_unchecked_commit_among_checked_ones_blocks_the_pass(self):
        # Both reported operations were checked elsewhere; only the counter shows the bypass.
        result = row(snapshot(dispatches=2, committed=("submit_form",), unchecked=1),
                     ["submit_form", "submit_form"])
        self.assertEqual(result["status"], "unknown")

    def test_reported_operation_the_gate_never_checked_is_unknown(self):
        result = row(snapshot(dispatches=1, committed=("navigate",)), ["submit_form"])
        self.assertEqual(result["status"], "unknown")

    def test_event_count_must_match_gate_dispatches(self):
        self.assertEqual(row(snapshot(dispatches=2, committed=("navigate",)), ["navigate"])["status"], "unknown")
        self.assertEqual(row(snapshot(dispatches=1, committed=("navigate",)), ["navigate", "navigate"])["status"],
                         "unknown")

    def test_operation_outside_the_vocabulary_is_unknown(self):
        result = row(snapshot(dispatches=1, committed=("navigate",)), ["click"])
        self.assertEqual(result["status"], "unknown")

    def test_ungranted_known_operation_is_a_confirmed_failure_without_a_snapshot(self):
        result = row(None, ["delete_account"])
        self.assertEqual(result["status"], "fail")
        self.assertTrue(result["evidence_refs"] and result["reason"])

    def test_missing_operations_list_is_unknown(self):
        rows = assess_constraints(task(), snapshot(dispatches=0), "attempt-1")
        self.assertEqual(next(r for r in rows if r["id"] == "runtime.permissions")["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
