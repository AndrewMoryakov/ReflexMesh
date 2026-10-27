"""The runtime.no_uncertain_repeats row: a confirmed repeat fails; absence of one does not pass."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.runner import AttemptSupervisor, WorkerResult
from reflexmesh.verification.constraints import assess_constraints
from test_v05_runtime import pass_verifier, sample


def task():
    data = sample()
    data["limits"]["wall_seconds"] = 30
    data["permissions"] = ["navigate", "type_text", "submit_form", "save_settings"]
    return ExecutionTask.from_dict(data)


def row(id_, operation="submit_form", target="send-form", slot=None):
    entry = {"id": id_, "operation": operation, "target_id": target}
    if slot is not None:
        entry["slot_ref"] = slot
    return entry


def repeats(dispatches, outcomes=None):
    rows = assess_constraints(task(), None, "attempt-1", dispatches=dispatches,
                              driver_outcomes={} if outcomes is None else outcomes)
    return next(r for r in rows if r["id"] == "runtime.no_uncertain_repeats")


class RepeatRow(unittest.TestCase):
    def test_absence_of_a_repeat_is_not_a_pass(self):
        result = repeats([row(1), row(2, "save_settings", "save")], {1: "ok", 2: "ok"})
        self.assertEqual(result["status"], "unknown")
        self.assertIn("not established", result["reason"])

    def test_no_dispatches_is_not_a_pass_either(self):
        self.assertEqual(repeats([])["status"], "unknown")

    def test_the_same_mutation_committed_twice_fails(self):
        result = repeats([row(1), row(2)], {1: "ok", 2: "ok"})
        self.assertEqual(result["status"], "fail")
        self.assertTrue(result["evidence_refs"] and result["reason"])

    def test_the_same_target_with_another_slot_is_a_different_mutation(self):
        result = repeats([row(1, "type_text", "name", "name@1"), row(2, "type_text", "name", "name@2")])
        self.assertEqual(result["status"], "unknown")

    def test_the_same_slot_typed_twice_fails(self):
        result = repeats([row(1, "type_text", "name", "name@1"), row(2, "type_text", "name", "name@1")])
        self.assertEqual(result["status"], "fail")

    def test_a_mutation_after_a_driver_error_fails(self):
        self.assertEqual(repeats([row(1), row(2, "save_settings", "save")], {1: "error"})["status"], "fail")

    def test_a_mutation_after_a_failed_return_fails(self):
        self.assertEqual(repeats([row(1), row(2, "save_settings", "save")], {1: "not_ok"})["status"], "fail")

    def test_an_uncertain_last_mutation_alone_is_not_a_violation(self):
        self.assertEqual(repeats([row(1)], {1: "error"})["status"], "unknown")

    def test_navigation_is_not_a_mutation(self):
        navigations = [row(1, "navigate", "/start"), row(2, "navigate", "/start")]
        self.assertEqual(repeats(navigations, {1: "error"})["status"], "unknown")
        after = [row(1, "navigate", "/start"), row(2)]
        self.assertEqual(repeats(after, {1: "error"})["status"], "unknown")

    def test_dispatches_are_judged_in_action_id_order(self):
        self.assertEqual(repeats([row(2, "save_settings", "save"), row(1)], {1: "error"})["status"], "fail")

    def test_missing_inputs_are_unknown_and_carry_no_failure(self):
        rows = assess_constraints(task(), None, "attempt-1")
        self.assertEqual(next(r for r in rows if r["id"] == "runtime.no_uncertain_repeats")["status"], "unknown")


def two_submissions(gate, events):
    ids = []
    for _ in range(2):
        gate.reserve_step()
        action_id, gate_seq = gate.commit_dispatch_seq(operation="submit_form")
        events.put(("proposal", {"action": "click"}))
        events.put(("dispatch", action_id, {"operation": "submit_form", "target_id": "send-form",
                                            "gate_seq": gate_seq}))
        events.put(("driver_return", action_id, True))
        gate.settle(action_id)
        ids.append(action_id)
    return WorkerResult("finish")


def mutation_after_error(gate, events):
    gate.reserve_step()
    first, seq = gate.commit_dispatch_seq(operation="submit_form")
    events.put(("proposal", {"action": "click"}))
    events.put(("dispatch", first, {"operation": "submit_form", "target_id": "send-form", "gate_seq": seq}))
    events.put(("effect_unknown", first))
    gate.settle(first)
    gate.reserve_step()
    second, seq = gate.commit_dispatch_seq(operation="save_settings")
    events.put(("proposal", {"action": "click"}))
    events.put(("dispatch", second, {"operation": "save_settings", "target_id": "save", "gate_seq": seq}))
    events.put(("driver_return", second, True))
    return WorkerResult("finish")


def mutation_after_failed_return(gate, events):
    gate.reserve_step()
    first, seq = gate.commit_dispatch_seq(operation="submit_form")
    events.put(("proposal", {"action": "click"}))
    events.put(("dispatch", first, {"operation": "submit_form", "target_id": "send-form", "gate_seq": seq}))
    events.put(("driver_return", first, False))
    gate.settle(first)
    gate.reserve_step()
    second, seq = gate.commit_dispatch_seq(operation="save_settings")
    events.put(("proposal", {"action": "click"}))
    events.put(("dispatch", second, {"operation": "save_settings", "target_id": "save", "gate_seq": seq}))
    events.put(("driver_return", second, True))
    return WorkerResult("finish")


def two_different_mutations(gate, events):
    for operation, target in (("submit_form", "send-form"), ("save_settings", "save")):
        gate.reserve_step()
        action_id, gate_seq = gate.commit_dispatch_seq(operation=operation)
        events.put(("proposal", {"action": "click"}))
        events.put(("dispatch", action_id, {"operation": operation, "target_id": target,
                                            "gate_seq": gate_seq}))
        events.put(("driver_return", action_id, True))
        gate.settle(action_id)
    return WorkerResult("finish")


def repeat_row(result):
    return next(r for r in result["verification"] if r["id"] == "runtime.no_uncertain_repeats")


class EndToEnd(unittest.TestCase):
    def test_a_repeated_submission_fails_the_attempt(self):
        result = AttemptSupervisor(task(), two_submissions, pass_verifier).run()
        self.assertEqual(repeat_row(result)["status"], "fail")
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("failed", "constraint_violated", "fail"))

    def test_a_mutation_after_an_uncertain_effect_fails_the_attempt(self):
        result = AttemptSupervisor(task(), mutation_after_error, pass_verifier).run()
        self.assertEqual(repeat_row(result)["status"], "fail")
        self.assertEqual(result["stop_reason"], "constraint_violated")

    def test_a_mutation_after_a_failed_return_fails_the_attempt(self):
        result = AttemptSupervisor(task(), mutation_after_failed_return, pass_verifier).run()
        self.assertEqual(repeat_row(result)["status"], "fail")
        self.assertEqual(result["stop_reason"], "constraint_violated")

    def test_distinct_mutations_do_not_fail_and_do_not_pass(self):
        result = AttemptSupervisor(task(), two_different_mutations, pass_verifier).run()
        self.assertEqual(repeat_row(result)["status"], "unknown")
        self.assertNotEqual(result["stop_reason"], "constraint_violated")


if __name__ == "__main__":
    unittest.main()
