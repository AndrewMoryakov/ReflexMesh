"""Completion-gate regressions. Synthetic pass rows test the gate, not adapter conformance."""

import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.contracts.task import ValidationError
from reflexmesh.runtime.cli import run_execution
from reflexmesh.runtime.runner import AttemptSupervisor, RuntimeStop, WorkerResult
from reflexmesh.verification.constraints import (
    REQUIRED_CONSTRAINTS, ConstraintAssessments, assess_constraints,
)
from test_v05_runtime import (
    bootstrap_then_finish, fail_verifier, finish_no_action, no_confident_action,
    pass_verifier, sample, synthetic_constraint_passes as synthetic_passes,
)


def task(*, unlimited=False):
    data = sample()
    data["limits"]["wall_seconds"] = 30
    if unlimited:
        data["limits"] = None
    return ExecutionTask.from_dict(data)


def missing_dispatch_event(gate, events):
    gate.reserve_step()
    gate.commit_dispatch()
    return WorkerResult("finish")


class AssessmentContract(unittest.TestCase):
    def test_fixed_required_set_is_complete_even_without_assessments(self):
        ledger = ConstraintAssessments()
        ledger.update([])
        self.assertEqual(tuple(row["id"] for row in ledger.rows()), (
            "runtime.permissions", "runtime.immutable_slots", "runtime.ownership",
            "runtime.dispatch_ordering", "runtime.no_uncertain_repeats", "runtime.budget"))
        self.assertFalse(ledger.all_pass())
        self.assertTrue(all(row["status"] == "unknown" and row["reason"] and
                            row["evidence_refs"] == [] for row in ledger.rows()))

    def test_each_missing_duplicate_or_evidence_free_pass_blocks_completion(self):
        for cid in REQUIRED_CONSTRAINTS:
            for defect in ("missing", "duplicate", "no_evidence", "wrong_kind"):
                with self.subTest(cid=cid, defect=defect):
                    rows = synthetic_passes()
                    target = next(row for row in rows if row["id"] == cid)
                    if defect == "missing":
                        rows.remove(target)
                    elif defect == "duplicate":
                        rows.append(dict(target))
                    elif defect == "no_evidence":
                        target.update(evidence_refs=[], observed_at=None)
                    else:
                        target["kind"] = "postcondition"
                    ledger = ConstraintAssessments()
                    ledger.update(rows)
                    self.assertFalse(ledger.all_pass())
                    self.assertEqual(next(row for row in ledger.rows() if row["id"] == cid)["status"],
                                     "unknown")

    def test_confirmed_failure_is_sticky_and_returned_rows_are_copies(self):
        for cid in REQUIRED_CONSTRAINTS:
            with self.subTest(cid=cid):
                rows = synthetic_passes()
                failure = next(row for row in rows if row["id"] == cid)
                failure.update(status="fail", reason="Confirmed test violation")
                ledger = ConstraintAssessments()
                ledger.update(rows)
                failure["status"] = "pass"
                for later in (synthetic_passes(), [], assess_constraints(task(), None, "attempt")):
                    ledger.update(later)
                    output = ledger.rows()
                    failed = next(row for row in output if row["id"] == cid)
                    self.assertEqual(failed["status"], "fail")
                    self.assertEqual(failed["reason"], "Confirmed test violation")
                    failed["status"] = "pass"
                    failed["evidence_refs"].clear()
                    self.assertTrue(ledger.has_fail())
                    self.assertFalse(ledger.all_pass())
                    self.assertTrue(next(row for row in ledger.rows() if row["id"] == cid)["evidence_refs"])

    def test_guards_do_not_supply_full_contract_evidence(self):
        rows = assess_constraints(task(), {"steps": 1, "model_calls": 1}, "attempt")
        self.assertEqual([row["status"] for row in rows], ["unknown"] * 5 + ["pass"])
        self.assertTrue(all(row["reason"] and row["observed_at"] is None and
                            row["evidence_refs"] == [] for row in rows[:-1]))

    def test_budget_scope_handles_disabled_limits_and_opaque_router_usage(self):
        for unlimited, count, expected in ((False, 3, "pass"), (False, 4, "fail"),
                                            (True, 100000, "pass")):
            with self.subTest(unlimited=unlimited, count=count):
                rows = assess_constraints(task(unlimited=unlimited), {
                    "steps": count, "model_calls": count, "opaque_model_usage": True}, "attempt")
                self.assertEqual(rows[-1]["status"], expected)
                self.assertEqual(rows[-1]["scope"], "steps_and_locally_controlled_model_calls")
                self.assertEqual(rows[-1]["evidence_refs"], ["runtime:attempt:budget"])
                self.assertEqual([row["status"] for row in rows[:-1]], ["unknown"] * 5)

    def test_unavailable_or_invalid_counter_evidence_is_unknown(self):
        for snapshot in (None, {}, {"steps": True, "model_calls": 0},
                         {"steps": 0, "model_calls": None}, {"steps": -1, "model_calls": 0}):
            with self.subTest(snapshot=snapshot):
                row = assess_constraints(task(), snapshot, "attempt")[-1]
                self.assertEqual((row["status"], row["evidence_refs"]), ("unknown", []))

    def test_task_cannot_override_required_set_or_claim_runtime_ids(self):
        for cid in REQUIRED_CONSTRAINTS:
            data = sample()
            data["criteria"][0]["id"] = cid
            with self.assertRaises(ValidationError):
                ExecutionTask.from_dict(data)
        for field in ("required_constraints", "execution_constraints"):
            data = sample()
            data[field] = []
            with self.assertRaises(ValidationError):
                ExecutionTask.from_dict(data)


class CompletionGate(unittest.TestCase):
    def test_postconditions_pass_with_missing_constraints_never_completes(self):
        result = AttemptSupervisor(task(), finish_no_action, pass_verifier).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("incomplete", "verification_unknown", "unknown"))
        self.assertEqual(result["verification"][0]["status"], "pass")
        self.assertEqual([row["status"] for row in result["verification"][1:]],
                         ["unknown"] * 5 + ["pass"])

    def test_all_required_passes_allow_both_normal_finish_kinds(self):
        for strategy in (finish_no_action, no_confident_action):
            with self.subTest(strategy=strategy.__name__), patch(
                    "reflexmesh.runtime.runner.assess_constraints", side_effect=synthetic_passes):
                result = AttemptSupervisor(task(), strategy, pass_verifier).run()
            self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                             ("completed", "verified", "pass"))

    def test_all_required_passes_preserve_already_satisfied_navigation(self):
        data = sample()
        data["limits"]["wall_seconds"] = 30
        data["criteria"] = [{"id": "page", "kind": "postcondition", "predicate": "current_page",
                             "args": {"path": "/form", "target_id": "form"}}]
        request = ExecutionTask.from_dict(data)
        with patch("reflexmesh.runtime.runner.assess_constraints", side_effect=synthetic_passes):
            result = AttemptSupervisor(request, lambda g, e: bootstrap_then_finish(g, e, request),
                                       pass_verifier).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]),
                         ("completed", "already_satisfied"))

    def test_completed_evidence_survives_missing_read_but_retains_late_failure(self):
        for later, outcome in (([], "pass"),
                               ([{**row, "status": "fail"} for row in synthetic_passes()], "fail")):
            with self.subTest(outcome=outcome), patch(
                    "reflexmesh.runtime.runner.assess_constraints",
                    side_effect=[synthetic_passes(), synthetic_passes(), later]) as assess:
                result = AttemptSupervisor(task(), finish_no_action, pass_verifier).run()
            self.assertEqual(assess.call_count, 3)
            self.assertEqual((result["attempt_status"], result["task_outcome"]), ("completed", outcome))
            self.assertTrue(all(row["status"] == outcome for row in result["verification"][1:]))

    def test_all_required_passes_do_not_bypass_other_completion_gates(self):
        for strategy, verifier, expected in (
            (finish_no_action, fail_verifier, ("failed", "postcondition_failed")),
            (finish_no_action, None, ("incomplete", "verification_unknown")),
            (missing_dispatch_event, pass_verifier, ("incomplete", "verification_unknown")),
        ):
            with self.subTest(expected=expected), patch(
                    "reflexmesh.runtime.runner.assess_constraints", side_effect=synthetic_passes):
                result = AttemptSupervisor(task(), strategy, verifier).run()
            self.assertEqual((result["attempt_status"], result["stop_reason"]), expected)

    def test_postcondition_verifier_cannot_install_constraint_passes(self):
        def claimed_passes(request, timeout):
            return pass_verifier(request, timeout) + synthetic_passes()

        result = AttemptSupervisor(task(), finish_no_action, claimed_passes).run()
        self.assertEqual(result["attempt_status"], "incomplete")
        self.assertEqual([row["status"] for row in result["verification"][:-1]], ["unknown"] * 6)

    def test_constraint_failure_blocks_completion_and_survives_later_pass(self):
        calls = 0

        def fail_then_pass(*args):
            nonlocal calls
            calls += 1
            rows = synthetic_passes()
            if calls == 1:
                rows[0]["status"] = "fail"
            return rows

        with patch("reflexmesh.runtime.runner.assess_constraints", side_effect=fail_then_pass):
            result = AttemptSupervisor(task(), finish_no_action, pass_verifier).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("failed", "constraint_violated", "fail"))
        self.assertEqual(result["verification"][1]["status"], "fail")

    def test_cancel_and_deadline_precedence_with_sticky_failure(self):
        for stop in ("cancel", "deadline"):
            for failed in (False, True):
                with self.subTest(stop=stop, failed=failed):
                    supervisor = AttemptSupervisor(task(), finish_no_action, pass_verifier)
                    calls = 0

                    def stop_during_assessment(*args):
                        nonlocal calls
                        calls += 1
                        rows = synthetic_passes()
                        if calls == 1:
                            if failed:
                                rows[0]["status"] = "fail"
                            if stop == "cancel":
                                self.assertTrue(supervisor.cancel())
                            else:
                                supervisor.gate.deadline = time.monotonic() - 1
                                self.assertFalse(supervisor.cancel())
                        return rows

                    with patch("reflexmesh.runtime.runner.assess_constraints", side_effect=stop_during_assessment):
                        result = supervisor.run()
                    self.assertEqual((result["attempt_status"], result["stop_reason"]),
                                     ("cancelled", "cancel_requested") if stop == "cancel" else
                                     ("incomplete", "deadline"))
                    self.assertEqual(result["task_outcome"], "fail" if failed else "unknown")
                    self.assertEqual(result["verification"][1]["status"], "fail" if failed else "pass")

    def test_unavailable_gate_snapshot_cannot_create_budget_pass(self):
        supervisor = AttemptSupervisor(task(), finish_no_action, pass_verifier)
        with patch.object(supervisor.gate, "snapshot", side_effect=RuntimeStop("gate_unavailable")):
            result = supervisor.run()
        self.assertEqual(result["attempt_status"], "incomplete")
        self.assertEqual(result["verification"][-1]["status"], "unknown")

    def test_late_budget_violation_preserves_accepted_deadline(self):
        supervisor = AttemptSupervisor(task(), finish_no_action, pass_verifier)
        supervisor.gate.deadline = time.monotonic() - 1
        cleanup = supervisor.process.cleanup

        def violate_after_stop(timeout):
            outcome = cleanup(timeout)
            # Fault injection after deadline acceptance, before final assessment.
            supervisor.gate.steps.value = supervisor.gate.max_steps + 1
            return outcome

        with patch.object(supervisor.process, "cleanup", side_effect=violate_after_stop):
            result = supervisor.run()
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("incomplete", "deadline", "fail"))
        self.assertEqual(result["verification"][-1]["status"], "fail")

    def test_late_synchronized_over_limit_snapshot_cannot_keep_completed_pass(self):
        supervisor = AttemptSupervisor(task(), finish_no_action, pass_verifier)
        cleanup = supervisor.process.cleanup

        def assess(request, snapshot, attempt_id):
            rows = synthetic_passes()
            rows[-1] = assess_constraints(request, snapshot, attempt_id)[-1]
            return rows

        def violate_after_completion(timeout):
            outcome = cleanup(timeout)
            supervisor.gate.calls.value = supervisor.gate.max_calls + 1
            return outcome

        with (patch("reflexmesh.runtime.runner.assess_constraints", side_effect=assess),
              patch.object(supervisor.process, "cleanup", side_effect=violate_after_completion)):
            result = supervisor.run()
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("completed", "verified", "fail"))
        self.assertEqual(result["verification"][-1]["status"], "fail")
        self.assertGreater(result["budget"]["local_model_calls"], result["budget"]["limits"]["max_model_calls"])


class ConstraintSerialization(unittest.TestCase):
    def args(self, output):
        return SimpleNamespace(input="task.json", output_dir=str(output), no_limits=True,
                               routing_provider="stub", action_provider="jev", script=None,
                               chrome=None, jev_url="http://127.0.0.1:8787", timeout=30.0)

    def test_cli_result_contains_all_assessments_and_only_real_evidence_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            stdout = io.StringIO()
            with (patch("reflexmesh.runtime.cli._load", return_value=sample()),
                  patch("reflexmesh.runtime.cli.BrowserExecutionStrategy", return_value=finish_no_action),
                  patch("reflexmesh.runtime.cli.signal.signal"), redirect_stdout(stdout)):
                self.assertEqual(run_execution(self.args(output)), 4)
            result = json.loads((output / "result.json").read_text())
            self.assertEqual(json.loads(stdout.getvalue()), result)
            rows = result["verification"][1:]
            self.assertEqual(tuple(row["id"] for row in rows), REQUIRED_CONSTRAINTS)
            self.assertTrue(all(row["reason"] and row["kind"] == "execution_constraint" for row in rows))
            self.assertTrue(all(row["evidence_refs"] == [] for row in rows[:-1]))
            evidence = [json.loads(line) for line in (output / "evidence.jsonl").read_text().splitlines()]
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0]["criterion_id"], "runtime.budget")
            self.assertEqual(evidence[0]["scope"], "steps_and_locally_controlled_model_calls")
            self.assertEqual(evidence[0]["reason"], rows[-1]["reason"])
            self.assertEqual(result["budget"]["limits"], {
                "wall_seconds": None, "max_steps": None, "max_model_calls": None})
            json.dumps(result, allow_nan=False)

    def test_output_failure_does_not_erase_confirmed_constraint_failure(self):
        result = {"attempt_status": "cancelled", "task_outcome": "fail", "actions": [], "trace": [],
                  "verification": [{**synthetic_passes()[0], "status": "fail"}]}
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as stdout:
            with (patch("reflexmesh.runtime.cli._load", return_value=sample()),
                  patch("reflexmesh.runtime.cli.AttemptSupervisor") as supervisor,
                  patch("reflexmesh.runtime.cli.signal.signal"),
                  patch.object(Path, "write_text", side_effect=OSError("output unavailable"))):
                supervisor.return_value.run.return_value = result
                self.assertEqual(run_execution(self.args(Path(directory) / "result")), 5)
            written = json.loads(stdout.getvalue())
            self.assertEqual((written["stop_reason"], written["task_outcome"]), ("output_error", "fail"))
            self.assertEqual(written["verification"][0]["status"], "fail")


if __name__ == "__main__":
    unittest.main()
