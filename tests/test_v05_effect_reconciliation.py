"""Effects and repeat evidence resolved from the authenticated fixture record."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.runner import AttemptSupervisor
from reflexmesh.verification.constraints import assess_constraints
from reflexmesh.verification.effects import repeat_verdict, resolve_effects, snapshot_of
from test_v05_constraints import EffectsVerifier, submit_once
from test_v05_runtime import sample

SAME = lambda action, evidence: action == evidence  # noqa: E731 - the supervisor's binding check, simplified


def record(paths=(), *, pending=0, ownership=None):
    return {"kind": "fixture_effect_snapshot", "source": "authenticated_fixture_response",
            "ref": "fixture:run:1", "ownership": ownership, "pending_effect_count": pending,
            "effects": [{"seq": seq, "method": "POST", "path": path, "binding": ownership}
                        for seq, path in enumerate(paths, start=1)]}


def action(id_, target="send-form", operation="submit_form", effect="unknown", ownership=None):
    row = {"id": id_, "operation": operation, "target_id": target, "effect": effect, "evidence_refs": []}
    if ownership is not None:
        row["ownership"] = ownership
    return row


def resolve(actions, snapshot, *, stopped=False, same=SAME):
    resolve_effects(actions, snapshot, same, browser_stopped=stopped)
    return [row["effect"] for row in actions]


class SnapshotShape(unittest.TestCase):
    def test_a_well_formed_record_is_found_among_rows(self):
        good = record(["/form"])
        self.assertIs(snapshot_of([{"fixture_snapshot": None}, {"fixture_snapshot": good}]), good)

    def test_malformed_records_are_ignored(self):
        for bad in ({**record(), "kind": "other"}, {**record(), "source": "worker"},
                    {**record(), "ref": ""}, {**record(), "pending_effect_count": -1},
                    {**record(), "pending_effect_count": True}, {**record(), "effects": "x"},
                    {**record(["/form", "/form"]), "effects": [{"seq": 1, "path": "/form"},
                                                                 {"seq": 1, "path": "/form"}]},
                    {**record(["/form"]), "effects": [{"seq": 1, "path": 7}]},
                    {**record(["/form"]), "effects": [{"seq": "1", "path": "/form"}]}):
            self.assertIsNone(snapshot_of([{"fixture_snapshot": bad}]), bad)
        self.assertIsNone(snapshot_of(None))


class ResolveEffects(unittest.TestCase):
    def test_equal_counts_with_nothing_pending_settle_the_effects_as_applied(self):
        rows = [action(1)]
        self.assertEqual(resolve(rows, record(["/form"])), ["applied"])
        self.assertEqual(rows[0]["evidence_refs"], ["fixture:run:1"])

    def test_two_targets_are_matched_independently(self):
        rows = [action(1), action(2, "save-settings", "save_settings")]
        self.assertEqual(resolve(rows, record(["/form", "/settings"])), ["applied", "applied"])
        rows = [action(1), action(2, "save-settings", "save_settings")]
        self.assertEqual(resolve(rows, record(["/form"])), ["applied", "unknown"])

    def test_no_request_is_not_applied_only_once_the_browser_is_known_to_be_stopped(self):
        self.assertEqual(resolve([action(1)], record()), ["unknown"])
        self.assertEqual(resolve([action(1)], record(), stopped=True), ["not_applied"])

    def test_pending_effects_settle_nothing(self):
        self.assertEqual(resolve([action(1)], record(["/form"], pending=1), stopped=True), ["unknown"])
        self.assertEqual(resolve([action(1)], record(pending=1), stopped=True), ["unknown"])

    def test_a_partial_or_excess_count_is_not_matched(self):
        self.assertEqual(resolve([action(1), action(2)], record(["/form"]), stopped=True), ["unknown", "unknown"])
        self.assertEqual(resolve([action(1)], record(["/form", "/form"]), stopped=True), ["unknown"])

    def test_a_settled_effect_is_never_unsettled(self):
        rows = [action(1, effect="applied"), action(2, effect="not_applied")]
        self.assertEqual(resolve(rows, record(), stopped=True), ["applied", "not_applied"])

    def test_navigation_and_typing_are_not_server_effects(self):
        rows = [action(1, "/start", "navigate"), action(2, "name", "type_text")]
        self.assertEqual(resolve(rows, record(), stopped=True), ["unknown", "unknown"])

    def test_a_navigation_row_is_never_matched_to_a_post_even_if_it_names_a_mutating_target(self):
        rows = [action(1, "send-form", "navigate")]
        self.assertEqual(resolve(rows, record(["/form"])), ["unknown"])
        self.assertIsNone(repeat_verdict(rows, record(["/form"])))

    def test_rows_of_another_owner_are_not_settled(self):
        theirs = {"claim_id": "other"}
        rows = [action(1, ownership={"claim_id": "mine"})]
        self.assertEqual(resolve(rows, record(["/form"], ownership=theirs)), ["unknown"])
        rows = [action(1, ownership=theirs)]
        self.assertEqual(resolve(rows, record(["/form"], ownership=theirs)), ["applied"])

    def test_without_a_record_nothing_changes(self):
        self.assertEqual(resolve([action(1)], None, stopped=True), ["unknown"])


class RepeatVerdict(unittest.TestCase):
    def test_exactly_one_request_per_committed_mutation_passes(self):
        verdict = repeat_verdict([action(1), action(2, "save-settings", "save_settings")],
                                 record(["/form", "/settings"]))
        self.assertEqual(verdict[0], "pass")

    def test_no_mutations_and_no_requests_passes(self):
        self.assertEqual(repeat_verdict([action(1, "/start", "navigate")], record())[0], "pass")

    def test_more_requests_than_commits_is_a_confirmed_repeat(self):
        self.assertEqual(repeat_verdict([action(1)], record(["/form", "/form"]))[0], "fail")

    def test_a_missing_request_is_not_decided(self):
        self.assertIsNone(repeat_verdict([action(1), action(2)], record(["/form"])))

    def test_pending_effects_are_not_decided(self):
        self.assertIsNone(repeat_verdict([action(1)], record(["/form"], pending=1)))

    def test_a_request_for_a_target_nothing_was_committed_for_is_not_decided(self):
        self.assertIsNone(repeat_verdict([action(1)], record(["/form", "/settings"])))

    def test_no_record_is_not_decided(self):
        self.assertIsNone(repeat_verdict([action(1)], None))


def repeat_row(dispatches, gate_dispatches, effects, outcomes=None):
    task = ExecutionTask.from_dict({**sample(), "permissions": ["navigate", "type_text", "submit_form"]})
    gate = {"steps": 1, "model_calls": 0, "dispatches": gate_dispatches}
    rows = assess_constraints(task, gate, "attempt-1", dispatches=dispatches,
                              driver_outcomes={} if outcomes is None else outcomes, effect_snapshot=effects)
    return next(r for r in rows if r["id"] == "runtime.no_uncertain_repeats")


class RepeatRow(unittest.TestCase):
    def test_the_fixture_record_alone_can_pass_the_row(self):
        result = repeat_row([action(1)], 1, record(["/form"]))
        self.assertEqual(result["status"], "pass")
        self.assertIn("fixture:run:1", result["evidence_refs"])

    def test_a_repeat_made_inside_the_driver_is_a_confirmed_failure(self):
        # One committed submission, two recorded requests: the boundary saw nothing wrong.
        self.assertEqual(repeat_row([action(1)], 1, record(["/form", "/form"]))["status"], "fail")

    def test_a_lost_dispatch_event_cannot_turn_into_a_failure_or_a_pass(self):
        # The gate committed two, the supervisor holds one row: the record cannot be matched.
        two = record(["/form", "/form"])
        self.assertEqual(repeat_row([action(1)], 2, two)["status"], "unknown")
        self.assertEqual(repeat_row([action(1)], 2, record(["/form"]))["status"], "unknown")

    def test_a_boundary_violation_wins_over_a_clean_record(self):
        rows = [action(1), action(2)]
        self.assertEqual(repeat_row(rows, 2, record(["/form"]))["status"], "fail")

    def test_without_a_record_the_row_stays_unknown(self):
        self.assertEqual(repeat_row([action(1)], 1, None)["status"], "unknown")


class EndToEnd(unittest.TestCase):
    def run_hung(self, requests, pending=0):
        return AttemptSupervisor(_task(), lambda g, e: submit_once(g, e, hang=True),
                                 EffectsVerifier("unknown", requests, pending)).run()

    def test_a_lost_return_is_settled_as_applied_and_the_outcome_is_known(self):
        result = self.run_hung(1)
        self.assertEqual(result["actions"][-1]["effect"], "applied")
        self.assertEqual(result["execution_outcome"], "returned")

    def test_a_verifier_without_a_fixture_claim_cannot_pass_the_repeat_row(self):
        # The record is exact (one request, one commit), but nothing binds it to our claim.
        result = AttemptSupervisor(_task(), submit_once, EffectsVerifier("pass", 1)).run()
        row = next(r for r in result["verification"] if r["id"] == "runtime.no_uncertain_repeats")
        self.assertEqual(row["status"], "unknown")
        # Effects settle under the synthetic no-claim contract, exactly as criteria do; a
        # constraint pass is stricter and needs the supervisor's own authenticated claim.
        self.assertEqual(result["actions"][0]["effect"], "applied")

    def test_pending_effects_leave_the_outcome_unknown(self):
        result = self.run_hung(1, pending=1)
        self.assertEqual(result["actions"][-1]["effect"], "unknown")
        self.assertEqual(result["execution_outcome"], "unknown")

    def test_an_excess_of_requests_is_not_settled(self):
        result = self.run_hung(2)
        self.assertEqual(result["actions"][-1]["effect"], "unknown")
        self.assertEqual(result["execution_outcome"], "unknown")


def _task():
    data = sample()
    data["limits"]["wall_seconds"] = 0.5
    data["permissions"] = ["navigate", "type_text", "submit_form"]
    return ExecutionTask.from_dict(data)


if __name__ == "__main__":
    unittest.main()
