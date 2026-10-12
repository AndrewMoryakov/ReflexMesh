"""Support fixture regression sources (HTTP and synthetic DOM; not browser acceptance)."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser
from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.handoff import build_handoff
from reflexmesh.verification.effects import repeat_verdict, resolve_effects
from reflexmesh.verification.verifier import FixtureVerifier, read_fixture
from test_v05_fixture import FixtureServerCase
from test_v05_runtime import sample


VALUES = {"name": "support-private-name", "email": "support-private@example.test",
          "message": " Please call back.\nKeep this exact message. "}


def support_task(origin=None, run_id=None):
    data = sample()
    if origin is not None:
        data["fixture"] = {"origin": origin, "run_id": run_id}
    data["start_path"] = "/support"
    data["permissions"] = ["navigate", "type_text", "submit_form"]
    data["text_slots"] = [{"id": key, "version": 1, "value": value} for key, value in VALUES.items()]
    data["criteria"] = [{"id": "sent", "kind": "postcondition", "predicate": "support_request_sent_once",
                         "args": {"name_slot": "name@1", "email_slot": "email@1"}}]
    return ExecutionTask.from_dict(data)


class SupportHTTP(FixtureServerCase):
    def setup_support(self):
        task = support_task(self.origin, self.run_id)
        owner, baseline = self.claim(task)
        return task, owner, baseline, FixtureVerifier(task, baseline, ownership=owner)

    def submit(self, owner, values=None):
        body = urllib.parse.urlencode(VALUES if values is None else values).encode()
        with self.request(owner, "/support", body) as response:
            self.assertEqual(response.status, 200)

    def test_page_is_owned_and_exposes_required_message(self):
        task, owner, baseline, verifier = self.setup_support()
        with self.request(owner, "/support") as response:
            page = response.read().decode()
        for target in ("support", "support-name", "support-email", "support-message", "send-support"):
            self.assertIn(target, page)
        self.assertIn("name='message' required", page)
        self.assertEqual(verifier(task, 1)[0]["status"], "fail")
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(self.origin + "/support", timeout=1)
        self.assertEqual(denied.exception.code, 403)
        body = urllib.parse.urlencode(VALUES).encode()
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(self.origin + "/support", data=body, timeout=1)
        self.assertEqual(denied.exception.code, 403)
        self.assertEqual(owner.read(1)["state"]["support_requests"], 0)

    def test_exact_owned_submission_is_redacted_and_reconciles_only_submit(self):
        task, owner, baseline, verifier = self.setup_support()
        # Exercise redaction of both independent form payloads in one snapshot.
        with self.request(owner, "/form", urllib.parse.urlencode(VALUES).encode()) as response:
            response.read()
        self.submit(owner)
        private = owner.read(1)
        self.assertEqual(private["state"]["support"], VALUES)
        self.assertEqual(private["state"]["support_requests"], baseline["state"]["support_requests"] + 1)
        events = [e for e in private["effects"] if e["path"] == "/support"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["binding"], owner.binding)
        self.assertGreater(events[0]["seq"], baseline["sequence"])
        row = verifier(task, 1)[0]
        self.assertEqual(row["status"], "pass")
        self.assertEqual(row["verified_operations"], ["submit_form"])
        public = read_fixture(task, 1)
        self.assertEqual(public["state"]["support"], {"submitted": True})
        self.assertEqual(public["state"]["form"], {"submitted": True})
        for value in (*VALUES.values(), owner.browser_secret):
            for retained in (public, private["log"], row, owner.export()):
                self.assertNotIn(json.dumps(value)[1:-1], json.dumps(retained))
        self.assertTrue(any(e["path"] == "/support" for record in owner.export()
                            for e in record.get("effects", [])))
        with self.request(owner, "/support") as response:
            page = response.read().decode()
        self.assertIn("Support request sent.", page)
        self.assertNotIn("name='message' required", page)

    def test_missing_or_blank_message_does_not_create_effect(self):
        task, owner, baseline, verifier = self.setup_support()
        for message in (None, "", " \t\n"):
            with self.subTest(message=message):
                values = {key: value for key, value in VALUES.items() if key != "message"}
                if message is not None:
                    values["message"] = message
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    self.request(owner, "/support", urllib.parse.urlencode(values).encode())
                self.assertEqual(rejected.exception.code, 400)
                current = owner.read(1)
                self.assertEqual(current["sequence"], baseline["sequence"])
                self.assertEqual(current["effects"], [])
                self.assertEqual(current["state"]["support_requests"], 0)
                self.assertIsNone(current["state"]["support"])
                self.assertEqual(verifier(task, 1)[0]["status"], "fail")

    def test_wrong_name_cannot_pass(self):
        task, owner, _, verifier = self.setup_support()
        self.submit(owner, {**VALUES, "name": "wrong name"})
        self.assertEqual(verifier(task, 1)[0]["status"], "fail")

    def test_wrong_email_cannot_pass(self):
        task, owner, _, verifier = self.setup_support()
        self.submit(owner, {**VALUES, "email": "wrong@example.test"})
        self.assertEqual(verifier(task, 1)[0]["status"], "fail")

    def test_undeclared_message_cannot_pass(self):
        task, owner, _, verifier = self.setup_support()
        self.submit(owner, {**VALUES, "message": "An invented message"})
        self.assertEqual(verifier(task, 1)[0]["status"], "fail")

    def test_message_may_use_any_declared_slot_without_special_slot_name(self):
        task = support_task(self.origin, self.run_id)
        task = replace(task, slots=tuple(slot for slot in task.slots if slot.id != "message"))
        owner, baseline = self.claim(task)
        self.submit(owner, {**VALUES, "message": VALUES["name"]})
        row = FixtureVerifier(task, baseline, ownership=owner)(task, 1)[0]
        self.assertEqual(row["status"], "pass")

    def test_public_baseline_cannot_prove_support_effect(self):
        task, owner, _, _ = self.setup_support()
        public = read_fixture(task, 1)
        self.submit(owner)
        self.assertEqual(FixtureVerifier(task, public)(task, 1)[0]["status"], "unknown")

    def test_released_attempts_submission_cannot_pass_a_new_claim(self):
        task, owner, _, verifier = self.setup_support()
        self.submit(owner)
        self.assertEqual(verifier(task, 1)[0]["status"], "pass")
        # This HTTP-only test owns no browser or profile requiring cleanup.
        owner.revoke(1)
        owner.release("closed", 1)
        next_owner, next_baseline = self.claim(task)
        self.assertEqual(next_baseline["state"]["support_requests"], 1)
        self.assertEqual(FixtureVerifier(task, next_baseline, ownership=next_owner)(task, 1)[0]["status"], "fail")
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request(owner, "/support", urllib.parse.urlencode(VALUES).encode())
        self.assertEqual(denied.exception.code, 403)

    def test_foreign_pending_and_malformed_evidence_stay_unknown(self):
        task, owner, baseline, verifier = self.setup_support()
        self.submit(owner)
        current = owner.read(1)
        foreign = copy.deepcopy(current)
        foreign["effects"][0]["binding"]["attempt_id"] = "foreign-attempt"
        pending = copy.deepcopy(current)
        pending["pending_effects"] = [{"path": "/support", "binding": owner.binding}]
        bad_count = copy.deepcopy(current)
        bad_count["state"]["support_requests"] = True
        for snapshot in (foreign, pending, bad_count):
            with self.subTest(snapshot=snapshot), patch.object(owner, "read", return_value=snapshot):
                self.assertEqual(verifier(task, 1)[0]["status"], "unknown")

    def test_support_effect_and_repeat_checks_use_its_own_path(self):
        task, owner, _, verifier = self.setup_support()
        self.submit(owner)
        snapshot = verifier(task, 1)[0]["fixture_snapshot"]
        actions = [{"id": 1, "operation": "submit_form", "target_id": "send-support",
                    "effect": "unknown", "ownership": owner.binding},
                   {"id": 2, "operation": "type_text", "target_id": "support-message",
                    "effect": "unknown", "ownership": owner.binding},
                   {"id": 3, "operation": "submit_form", "target_id": "send-form",
                    "effect": "unknown", "ownership": owner.binding}]
        resolve_effects(actions, snapshot, lambda left, right: left == right, browser_stopped=False)
        self.assertEqual([a["effect"] for a in actions], ["applied", "unknown", "unknown"])
        self.assertEqual(repeat_verdict(actions[:2], snapshot)[0], "pass")
        self.submit(owner)
        row = verifier(task, 1)[0]
        self.assertEqual(row["status"], "fail")
        self.assertEqual(repeat_verdict(actions[:2], row["fixture_snapshot"])[0], "fail")


class SupportBackend:
    """Synthetic selector-map seam; no browser, SDK or ownership acceptance."""
    def __init__(self, origin):
        self.url = origin + "/support"
        self._session = self
        self.nodes = {}
        for index, target in enumerate(("support-name", "support-email", "support-message")):
            attrs = {"data-reflex-id": target, "type": "text"}
            if target == "support-message":
                attrs["required"] = ""
            self.nodes[index] = SimpleNamespace(
                backend_node_id=100 + index, frame_id="frame-1", target_id="tab-1", is_visible=True,
                tag_name="textarea" if target == "support-message" else "input", attributes=attrs,
                snapshot_node=SimpleNamespace(input_value=""))
        self.nodes[3] = SimpleNamespace(
            backend_node_id=103, tag_name="button", attributes={"data-reflex-id": "send-support"})

    def get_selector_map(self):
        return self.nodes

    def _run(self, value):
        return value

    def observe(self):
        return SimpleNamespace(fields={"url": self.url},
                               candidates={"text_fields": {str(i): "field" for i in range(3)},
                                           "elements": {"3": "Send"}, "text_values": {}})


class SupportAdapter(unittest.TestCase):
    def adapter(self):
        task = support_task()
        adapter = FixtureBrowser(task)
        adapter.backend = SupportBackend(task.origin)
        # Deliberately synthetic: no claim or controller exclusivity is asserted.
        adapter._assert_ownership = lambda **kwargs: None
        return adapter

    def test_support_candidates_and_permissions(self):
        adapter = self.adapter()
        obs = adapter.observe()
        self.assertEqual(set(obs.candidates["text_fields"]), {"0", "1", "2"})
        self.assertEqual(set(obs.candidates["elements"]), {"3"})
        self.assertEqual(adapter.admit("navigate", {"bootstrap": True}), None)
        self.assertEqual(adapter.admit("click", {"element": "3"}), None)
        self.assertEqual(adapter.describe("click", {"element": "3"})["operation"], "submit_form")
        self.assertEqual(adapter.admit("type_text", {"field": "2", "value": "message@1"}), None)
        self.assertEqual(adapter.admit("type_text", {"field": "2", "value": "invented@1"}), "invalid_action")
        adapter.task = replace(adapter.task, permissions=adapter.task.permissions - {"submit_form"})
        self.assertEqual(adapter.admit("click", {"element": "3"}), "policy_denied")
        self.assertEqual(adapter.observe().candidates["elements"], {})

    def test_required_empty_live_value_yields_content_handoff(self):
        adapter = self.adapter()
        for index, key in ((0, "name"), (1, "email")):
            adapter.backend.nodes[index].snapshot_node.input_value = VALUES[key]
        obs = adapter.observe()
        self.assertEqual(obs.fields["filled_targets"], ["support-email", "support-name"])
        self.assertEqual(obs.fields["empty_targets"], ["support-message"])
        self.assertEqual(obs.fields["content_gaps"], ["support-message"])
        result = {"attempt_status": "failed", "stop_reason": "postcondition_failed",
                  "task_outcome": "fail", "trace": [{"kind": "observation", "data": [obs.fields]}],
                  "actions": [], "verification": []}
        handoff = build_handoff(adapter.task, result, root_limits=sample()["limits"], usage={})
        self.assertEqual(handoff["kind"], "needs_content")
        self.assertEqual(handoff["needs"][0]["target_id"], "support-message")

    def test_live_value_is_private_and_tied_to_observed_node(self):
        adapter = self.adapter()
        before = adapter.observe().fields
        node = adapter.backend.nodes[2]
        node.snapshot_node.input_value = VALUES["message"]
        after = adapter.observe().fields
        self.assertIn("support-message", after["filled_targets"])
        self.assertNotIn("support-message", after["empty_targets"])
        self.assertEqual(after["content_gaps"], [])
        self.assertEqual(before["target_nodes"]["support-message"], after["target_nodes"]["support-message"])
        self.assertEqual(after["target_nodes"]["support-message"],
                         {"backend_node_id": 102, "frame_id": "frame-1", "target_id": "tab-1"})
        for value in VALUES.values():
            self.assertNotIn(value, json.dumps(after))
        node.backend_node_id = 999
        self.assertEqual(adapter.admit("type_text", {"field": "2", "value": "message@1"}), "stale_target")
        self.assertNotEqual(adapter.observe().fields["target_nodes"]["support-message"],
                            before["target_nodes"]["support-message"])

    def test_missing_live_value_is_not_empty_or_filled(self):
        adapter = self.adapter()
        node = adapter.backend.nodes[2]
        node.snapshot_node.input_value = None
        node.attributes["value"] = "static value must not become evidence"
        fields = adapter.observe().fields
        for key in ("filled_targets", "empty_targets", "content_gaps", "target_nodes"):
            self.assertNotIn("support-message", fields[key])

    def test_disabled_readonly_or_hidden_field_is_not_a_content_gap(self):
        for attribute in ("disabled", "readonly", "aria-disabled", "hidden"):
            with self.subTest(attribute=attribute):
                adapter = self.adapter()
                node = adapter.backend.nodes[2]
                if attribute == "hidden":
                    node.is_visible = False
                else:
                    node.attributes[attribute] = "true" if attribute == "aria-disabled" else ""
                fields = adapter.observe().fields
                for key in ("filled_targets", "empty_targets", "content_gaps", "target_nodes"):
                    self.assertNotIn("support-message", fields[key])

    def test_node_identity_does_not_retain_arbitrary_strings(self):
        adapter = self.adapter()
        node = adapter.backend.nodes[2]
        node.frame_id = "https://user:password@example.test"
        self.assertNotIn("support-message", adapter.observe().fields["target_nodes"])


if __name__ == "__main__":
    unittest.main()
