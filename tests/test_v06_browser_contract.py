"""browser-task/0.1 (V0.6 section 5): strict input, no fixture, honest unsupported lists."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reflexmesh.contracts.browser import BrowserTask, OPERATION_BITS, PERMISSIONS, canonical_origin
from reflexmesh.contracts.task import ValidationError
from v06_samples import task_dict


class BrowserTaskContract(unittest.TestCase):
    def invalid(self, **changes):
        with self.assertRaises(ValidationError):
            BrowserTask.from_dict(task_dict(**changes))

    def test_valid_task_parses_and_has_no_fixture(self):
        task = BrowserTask.from_dict(task_dict())
        self.assertEqual(task.slot_references, ("order@1", "note@1"))
        self.assertEqual(task.unsupported_criteria, ())
        self.assertEqual(task.unsupported_constraints, ())
        data = task_dict()
        data["fixture"] = {"origin": "http://127.0.0.1:1", "run_id": "x"}
        with self.assertRaises(ValidationError):
            BrowserTask.from_dict(data)

    def test_exact_field_set(self):
        for key in task_dict():
            data = task_dict()
            del data[key]
            with self.assertRaises(ValidationError, msg=key):
                BrowserTask.from_dict(data)
        data = task_dict()
        data["extra"] = 1
        with self.assertRaises(ValidationError):
            BrowserTask.from_dict(data)

    def test_json_input_is_strict(self):
        text = json.dumps(task_dict(), ensure_ascii=False)
        self.assertEqual(BrowserTask.from_json(text).task_id, "order-note-125")
        self.assertEqual(BrowserTask.from_json(text.encode("utf-8")).revision, 1)
        for bad in (text.replace('"revision": 1', '"revision": 1, "revision": 2'),
                    text.replace('"wall_seconds": 120', '"wall_seconds": NaN'),
                    text.replace('"wall_seconds": 120', '"wall_seconds": Infinity'),
                    " " * (64 * 1024 + 1) + text, "{not json"):
            with self.assertRaises(ValidationError, msg=bad[:40]):
                BrowserTask.from_json(bad)
        with self.assertRaises(ValidationError):
            BrowserTask.from_json(b"\xff\xfe")

    def test_start_url_and_origins(self):
        for url in ("ftp://127.0.0.1:8770/", "http://user:pw@127.0.0.1:8770/orders", "/orders",
                    "http://127.0.0.1:8770/orders#frag", "http://other:8770/orders"):
            self.invalid(start_url=url)
        for origins in ([], ["http://127.0.0.1:8770/"], ["HTTP://127.0.0.1:8770"], ["http://127.0.0.1:80"],
                        ["http://127.0.0.1:8770", "http://127.0.0.1:8770"], ["http://127.0.0.1:9999"]):
            self.invalid(allowed_origins=origins)
        self.assertEqual(canonical_origin("https://Example.com:443/a?b#c"), "https://example.com")
        self.assertIsNone(canonical_origin("javascript:alert(1)"))

    def test_permissions_are_the_closed_browser_vocabulary(self):
        self.assertTrue({"click", "type_text", "scroll"} <= PERMISSIONS)
        self.assertFalse({"finish", "escalate", "focus_region", "evaluate", "hold_key"} & PERMISSIONS)
        self.assertEqual(len(set(OPERATION_BITS.values())), len(OPERATION_BITS))
        self.invalid(permissions=["click", "evaluate"])
        self.invalid(permissions=["click", "click"])
        self.invalid(permissions=["finish"])

    def test_limits_are_finite_and_complete(self):
        base = task_dict()["limits"]
        for change in ({"wall_seconds": None}, {"wall_seconds": 0}, {"wall_seconds": float("inf")},
                       {"wall_seconds": True}, {"max_steps": 0}, {"max_steps": None}, {"max_model_calls": -1},
                       {"max_action_retries": 1}, {"max_handoffs": -1}, {"max_handoffs": None}):
            self.invalid(limits={**base, **change})
        for key in base:
            self.invalid(limits={k: v for k, v in base.items() if k != key})
        self.assertEqual(BrowserTask.from_dict(task_dict(limits={**base, "max_handoffs": 0})).limits.max_handoffs, 0)

    def test_slots(self):
        slot = {"id": "pw", "version": 2, "value": "S3cret", "sensitivity": "secret"}
        task = BrowserTask.from_dict(task_dict(text_slots=[slot], criteria=[
            {"id": "c", "predicate": "page_match",
             "args": {"anchor_slot_ref": "pw@2", "field_name": "x", "expected_slot_ref": "pw@2"}}]))
        self.assertEqual(task.slots[0].reference, "pw@2")
        self.assertNotIn("S3cret", repr(task.slots[0]))
        for bad in ({**slot, "sensitivity": "public"}, {**slot, "id": "a@b"}, {**slot, "value": 5},
                    {**slot, "version": 0}, {**slot, "extra": 1}):
            self.invalid(text_slots=[bad], criteria=task_dict()["criteria"])
        self.invalid(text_slots=[slot, dict(slot)])

    def test_criteria_and_constraints(self):
        self.invalid(criteria=[])
        self.invalid(criteria=[{"id": "c", "predicate": "page_match", "args": {}}])
        self.invalid(criteria=[{"id": "c", "predicate": "page_match",
                                "args": {"anchor_slot_ref": "ghost@1", "field_name": "x",
                                         "expected_slot_ref": "note@1"}}])
        self.invalid(criteria=[{"id": "runtime.budget", "predicate": "page_match", "args": {
            "anchor_slot_ref": "order@1", "field_name": "x", "expected_slot_ref": "note@1"}}])
        both = task_dict()["criteria"] * 2
        self.invalid(criteria=both)
        # An unknown predicate is a well-formed request this build cannot honour.
        task = BrowserTask.from_dict(task_dict(criteria=[{"id": "x", "predicate": "visual_match", "args": {}}]))
        self.assertEqual(task.unsupported_criteria, ("x",))
        # A constraint names a rule code must enforce; none is registered, so it is reported, not run.
        task = BrowserTask.from_dict(task_dict(constraints=[{"id": "no-buy", "predicate": "never_purchase",
                                                             "args": {}}]))
        self.assertEqual(task.unsupported_constraints, ("no-buy",))
        self.invalid(constraints=[{"id": "note_saved", "predicate": "never_purchase", "args": {}}])


if __name__ == "__main__":
    unittest.main()
