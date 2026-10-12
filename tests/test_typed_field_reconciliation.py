"""Conservative browser evidence for session-local typing, without running a worker."""

import queue
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.runtime.runner import AttemptSupervisor, ControlledEnvironment

URL = "http://127.0.0.1:1/form"
OWNER = {"claim_id": "claim-1", "session_id": "session-1", "profile_id": "profile-1"}
NODE = {"backend_node_id": 12, "frame_id": "frame-1", "target_id": "tab-1"}


def supervisor(binding=None):
    value = AttemptSupervisor.__new__(AttemptSupervisor)
    value.task = SimpleNamespace(origin="http://127.0.0.1:1", start_path="/form")
    value.attempt_id = "typed-evidence"
    value.fixture_owner = SimpleNamespace(
        acquisition_attempted=SimpleNamespace(value=binding is not None), verified_binding=binding)
    value.trace = []
    value.observation = None
    value._driver_outcomes = {}
    value._typing_observations = {}
    return value


def observation(filled=(), **extra):
    return {"url": URL, "filled_targets": list(filled), **extra}


def dispatch(value, rows, *, before=None, returned=True, descriptor=None):
    value._record(("observation", observation() if before is None else before), rows)
    value._record(("dispatch", 1, {"operation": "type_text", "target_id": "name",
                                    "slot_ref": "name@1", **(descriptor or {})}), rows)
    if returned is not None:
        value._record(("driver_return", 1, returned), rows)


class TypedFieldEvidence(unittest.TestCase):
    def test_form_criteria_only_settle_their_own_submission_target(self):
        for predicate, expected in (("support_request_sent_once", "send-support"),
                                    ("form_submitted_once", "send-form")):
            with self.subTest(predicate=predicate):
                value = supervisor(OWNER)
                value.task.criteria = [SimpleNamespace(predicate=predicate)]
                rows = [{"id": i, "operation": "submit_form", "target_id": target,
                         "ownership": OWNER, "effect": "unknown", "evidence_refs": []}
                        for i, target in enumerate(("send-form", "send-support", None), 1)]
                value._reconcile(rows, [{"status": "pass", "ownership": OWNER,
                                         "verified_operations": ["submit_form"],
                                         "evidence_refs": ["fixture:bound"]}])
                for row in rows:
                    self.assertEqual(row["effect"], "applied" if row["target_id"] == expected else "unknown")

    def test_claimed_field_requires_fresh_empty_value_and_same_dom_identity(self):
        value, rows = supervisor(OWNER), []
        before = observation(empty_targets=["name"], target_nodes={"name": dict(NODE)}, ownership=OWNER)
        dispatch(value, rows, before=before, descriptor={"ownership": OWNER})
        value._record(("observation", observation(["name"], target_nodes={"name": dict(NODE)},
                                                   ownership=OWNER)), rows)
        self.assertEqual(rows[0]["effect"], "applied")

    def test_claimed_missing_empty_value_identity_or_owner_stays_unknown(self):
        good_before = observation(empty_targets=["name"], target_nodes={"name": dict(NODE)}, ownership=OWNER)
        good_after = observation(["name"], target_nodes={"name": dict(NODE)}, ownership=OWNER)
        cases = [({**good_before, "empty_targets": []}, good_after),
                 ({**good_before, "target_nodes": {}}, good_after),
                 (good_before, {**good_after, "target_nodes": {}}),
                 (good_before, {**good_after, "target_nodes": {"name": {**NODE, "backend_node_id": 13}}}),
                 (good_before, {**good_after, "ownership": {**OWNER, "session_id": "other"}}),
                 ({**good_before, "ownership": None}, good_after)]
        for before, after in cases:
            with self.subTest(before=before, after=after):
                value, rows = supervisor(OWNER), []
                dispatch(value, rows, before=before, descriptor={"ownership": OWNER})
                value._record(("observation", after), rows)
                self.assertEqual(rows[0]["effect"], "unknown")

    def test_fresh_filled_field_settles_successful_input(self):
        value, rows = supervisor(), []
        dispatch(value, rows)
        value._record(("observation", observation(["name"])), rows)
        self.assertEqual(rows[0]["effect"], "applied")
        self.assertEqual(rows[0]["evidence_refs"], ["browser:typed-evidence:4"])

    def test_missing_failed_or_error_return_remains_unknown(self):
        for returned in (None, False):
            with self.subTest(returned=returned):
                value, rows = supervisor(), []
                dispatch(value, rows, returned=returned)
                value._record(("observation", observation(["name"])), rows)
                self.assertEqual(rows[0]["effect"], "unknown")
        value, rows = supervisor(), []
        dispatch(value, rows, returned=None)
        value._record(("effect_unknown", 1), rows)
        value._record(("observation", observation(["name"])), rows)
        self.assertEqual(rows[0]["effect"], "unknown")

    def test_observation_before_return_is_not_fresh_evidence(self):
        value, rows = supervisor(), []
        dispatch(value, rows, returned=None)
        value._record(("observation", observation(["name"])), rows)
        value._record(("driver_return", 1, True), rows)
        value._reconcile_typing(rows)
        self.assertEqual(rows[0]["effect"], "unknown")

    def test_prefilled_or_unobserved_target_does_not_prove_a_change(self):
        for before in (observation(["name"]), {"url": URL}):
            value, rows = supervisor(), []
            dispatch(value, rows, before=before)
            value._record(("observation", observation(["name"])), rows)
            self.assertEqual(rows[0]["effect"], "unknown")

    def test_other_field_page_or_missing_filled_state_is_not_evidence(self):
        for after in (observation(["email"]), observation(["name"], url=URL + "/other"),
                      {"url": URL}, {"url": URL, "filled_targets": "name"}):
            value, rows = supervisor(), []
            dispatch(value, rows)
            value._record(("observation", after), rows)
            self.assertEqual(rows[0]["effect"], "unknown")

    def test_intervening_dispatch_prevents_attributing_later_field_state(self):
        value, rows = supervisor(), []
        dispatch(value, rows)
        value._record(("dispatch", 2, {"operation": "submit_form", "target_id": "send-form"}), rows)
        value._record(("observation", observation(["name"])), rows)
        self.assertEqual(rows[0]["effect"], "unknown")
        self.assertEqual(rows[1]["effect"], "unknown")

    def test_non_typing_actions_are_not_settled_by_filled_fields(self):
        value, rows = supervisor(), []
        dispatch(value, rows, descriptor={"operation": "submit_form"})
        value._record(("observation", observation(["name"])), rows)
        self.assertEqual(rows[0]["effect"], "unknown")

    def test_observation_trace_contains_identifiers_not_field_values(self):
        value = ControlledEnvironment.__new__(ControlledEnvironment)
        value.events = queue.Queue()
        fields = {"url": URL, "filled_targets": ["name"], "content_gaps": ["message"],
                  "empty_targets": ["message"], "target_nodes": {"name": dict(NODE)},
                  "input_value": "private-value", "text": "private-value"}
        value._record_observation(SimpleNamespace(fields=fields))
        fields["filled_targets"].append("later-mutation")
        fields["target_nodes"]["name"]["backend_node_id"] = 99
        event = value.events.get_nowait()
        self.assertEqual(event[1]["filled_targets"], ["name"])
        self.assertEqual(event[1]["content_gaps"], ["message"])
        self.assertEqual(event[1]["target_nodes"]["name"], NODE)
        self.assertNotIn("private-value", repr(event))

    def test_malformed_field_metadata_is_not_copied_to_trace(self):
        value = ControlledEnvironment.__new__(ControlledEnvironment)
        value.events = queue.Queue()
        value._record_observation(SimpleNamespace(fields={
            "url": URL, "filled_targets": {"name": "private-value"},
            "target_nodes": {"name": {**NODE, "frame_id": "private value"}},
            "content_gaps": [{"value": "private-value"}]}))
        event = value.events.get_nowait()
        self.assertNotIn("filled_targets", event[1])
        self.assertNotIn("content_gaps", event[1])
        self.assertEqual(event[1]["target_nodes"], {})
        self.assertNotIn("private-value", repr(event))


if __name__ == "__main__":
    unittest.main()
