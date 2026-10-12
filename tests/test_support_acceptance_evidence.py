"""A07 uses redacted evidence rather than requiring plaintext in public POST logs."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/v06"))

from run_mcp_b import support_evidence_matches


class SupportAcceptanceEvidence(unittest.TestCase):
    def sample(self):
        return ({"state": {"support_requests": 1, "support": {"submitted": True}},
                 "log": [{"method": "POST", "path": "/support"}]},
                {"verification": [{"id": "sent", "kind": "postcondition", "status": "pass"},
                                  {"id": "runtime.immutable_slots", "kind": "constraint", "status": "pass"}],
                 "actions": [{"operation": "type_text", "target_id": "support-message",
                              "slot_ref": "message@1", "effect": "applied"}]})

    def test_redacted_log_and_bound_assessments_are_sufficient(self):
        state, result = self.sample()
        self.assertTrue(support_evidence_matches(state, result))
        self.assertNotIn("data", state["log"][0])

    def test_missing_or_uncertain_assessment_does_not_pass(self):
        state, result = self.sample()
        for index in (0, 1):
            for status in ("unknown", "fail"):
                changed = copy.deepcopy(result)
                changed["verification"][index]["status"] = status
                self.assertFalse(support_evidence_matches(state, changed))
            changed = copy.deepcopy(result)
            del changed["verification"][index]
            self.assertFalse(support_evidence_matches(state, changed))

    def test_other_slot_or_unresolved_input_does_not_pass(self):
        state, result = self.sample()
        for change in ({"slot_ref": "name@1"}, {"effect": "unknown"}, {"target_id": "name"}):
            changed = copy.deepcopy(result)
            changed["actions"][0].update(change)
            self.assertFalse(support_evidence_matches(state, changed))

    def test_absent_duplicate_or_rejected_posts_do_not_pass(self):
        state, result = self.sample()
        for log in ([], state["log"] * 2, [{**state["log"][0], "rejected": True}]):
            self.assertFalse(support_evidence_matches({**state, "log": log}, result))
        for count in (0, 2, True, None):
            self.assertFalse(support_evidence_matches({**state, "state": {"support_requests": count}}, result))


if __name__ == "__main__":
    unittest.main()
