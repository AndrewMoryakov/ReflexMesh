"""Fixture target identity checks with a simulated backend; real-browser checks live in
tests/browser and run only where Browser Use and Chromium are installed."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.adapters.system_one.fixture_browser import CONTROLS, LINKS, FixtureBrowser
from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.runner import RuntimeStop
from reflexmesh.runtime.runner import AttemptSupervisor, ControlledEnvironment, WorkerResult
from test_v05_runtime import sample, pass_verifier


def rejected_candidate(gate, events, adapter, action, params):
    env = ControlledEnvironment(adapter, gate, events, adapter.admit)
    env.observe()
    env.execute(action, params)
    return WorkerResult("finish")


def stale_candidate(gate, events, adapter):
    env = ControlledEnvironment(adapter, gate, events, adapter.admit)
    env.observe()
    adapter.backend.nodes[0].backend_node_id += 1
    skipped = env.execute("click", {"element": "0"})
    assert not skipped.ok
    env.observe()
    return WorkerResult("no_confident_action")


def revoked_after_selection(gate, events, adapter):
    """C13 shape: the policy is revoked after revalidation, before the dispatch commit."""
    env = ControlledEnvironment(adapter, gate, events, adapter.admit,
                                before_commit=lambda d: gate.revoke(d["operation"]))
    env.observe()
    env.execute("click", {"element": "0"})
    return WorkerResult("finish")


class FakeBackend:
    def __init__(self, url):
        self.url = url
        self.document = "doc-1"
        self.child_frames = 0
        self.facts = {}
        self.nodes = {0: SimpleNamespace(attributes={"data-reflex-id": "send-form"},
                                         backend_node_id=15, tag_name="button")}
        self._session = self

    def get_selector_map(self):
        return self.nodes

    def _run(self, value):
        return value

    def observe(self):
        return SimpleNamespace(fields={"url": self.url},
                               candidates={"elements": {"0": "button 'Send'", "1": "Other"}})

    def identity(self):
        return {"target_id": "T1", "frame_id": "T1", "document_id": self.document, "url": self.url,
                "child_frames": self.child_frames}

    def properties(self, ids, origin):
        out = {}
        for node in self.nodes.values():
            if node.backend_node_id not in ids:
                continue
            rid = (node.attributes or {}).get("data-reflex-id")
            control = CONTROLS.get(rid)
            out[node.backend_node_id] = {
                "connected": True, "reflex_id": rid, "tag": node.tag_name,
                "type": control[1] if control else "",
                "href": origin + LINKS[rid] if rid in LINKS else None, "disabled": False,
                "form_action": origin + control[2] if control else None,
                "form_method": control[3] if control else None,
                "duplicates": sum((n.attributes or {}).get("data-reflex-id") == rid for n in self.nodes.values()),
                "top_frame": True, **self.facts.get(node.backend_node_id, {})}
        return out


class AdapterContract(unittest.TestCase):
    def setup_adapter(self):
        data = sample()
        # Supervisor tests here fork a worker and a verifier; 0.3 s is too tight under load.
        data["limits"]["wall_seconds"] = 5
        task = ExecutionTask.from_dict(data)
        adapter = FixtureBrowser.__new__(FixtureBrowser)
        adapter.task = task
        adapter.backend = FakeBackend(task.origin + "/form")
        adapter.targets = {}
        adapter.page = None
        adapter.unsupported = False
        adapter.observation_url = ""
        adapter._identity = adapter.backend.identity
        adapter._properties = lambda ids: adapter.backend.properties(ids, task.origin)
        return adapter

    def test_filtered_candidate_and_node_replacement(self):
        adapter = self.setup_adapter()
        obs = adapter.observe()
        self.assertEqual(set(obs.candidates["elements"]), {"0"})
        self.assertEqual(obs.fields["document_id"], "doc-1")
        self.assertEqual(adapter.admit("click", {"element": "0"}), None)
        adapter.backend.nodes[0].backend_node_id = 16
        self.assertEqual(adapter.admit("click", {"element": "0"}), "stale_target")

    def test_missing_identity_fails_closed(self):
        adapter = self.setup_adapter()
        adapter.backend.nodes[0].backend_node_id = None
        with self.assertRaises(RuntimeStop) as error:
            adapter.observe()
        self.assertEqual(error.exception.reason, "adapter_contract_unsupported")

    def test_node_without_attributes_fails_closed(self):
        adapter = self.setup_adapter()
        adapter.backend.nodes[1] = SimpleNamespace(attributes=None, backend_node_id=20, tag_name="div")
        with self.assertRaises(RuntimeStop) as error:
            adapter.observe()
        self.assertEqual(error.exception.reason, "adapter_contract_unsupported")

    def test_frames_fail_closed(self):
        adapter = self.setup_adapter()
        adapter.backend.child_frames = 1
        with self.assertRaises(RuntimeStop) as error:
            adapter.observe()
        self.assertEqual(error.exception.reason, "adapter_contract_unsupported")
        adapter.backend.child_frames = 0
        adapter.backend.nodes[0].frame_id = "child-frame"
        with self.assertRaises(RuntimeStop):
            adapter.observe()

    def test_ambiguous_id_and_unexpected_parameters_fail_closed(self):
        adapter = self.setup_adapter()
        adapter.observe()
        self.assertEqual(adapter.admit("click", {"element": "0", "extra": 1}), "invalid_action")
        adapter.backend.nodes[1] = SimpleNamespace(attributes={"data-reflex-id": "send-form"},
                                                   backend_node_id=20, tag_name="button")
        with self.assertRaises(RuntimeStop) as error:
            adapter.observe()
        self.assertEqual(error.exception.reason, "adapter_contract_unsupported")

    def test_page_change_before_dispatch_is_stale(self):
        adapter = self.setup_adapter()
        adapter.observe()
        adapter.backend.url = "https://external.example/form"
        self.assertEqual(adapter.admit("click", {"element": "0"}), "stale_target")

    def test_new_document_with_same_url_is_stale(self):
        adapter = self.setup_adapter()
        adapter.observe()
        adapter.backend.document = "doc-2"
        self.assertEqual(adapter.admit("click", {"element": "0"}), "stale_target")

    def test_disabled_target_is_not_offered_and_disabling_is_stale(self):
        adapter = self.setup_adapter()
        adapter.observe()
        adapter.backend.facts[15] = {"disabled": True}
        self.assertEqual(adapter.admit("click", {"element": "0"}), "stale_target")
        obs = adapter.observe()
        self.assertEqual(obs.candidates["elements"], {})
        self.assertEqual(adapter.admit("click", {"element": "0"}), "invalid_action")

    def test_form_endpoint_must_match_fixture_definition(self):
        adapter = self.setup_adapter()
        adapter.observe()
        adapter.backend.facts[15] = {"form_action": adapter.task.origin + "/danger/delete"}
        self.assertEqual(adapter.admit("click", {"element": "0"}), "stale_target")
        obs = adapter.observe()
        self.assertEqual(obs.candidates["elements"], {})
        self.assertEqual(adapter.admit("click", {"element": "0"}), "policy_denied")

    def test_reindexed_node_reobserved_without_dispatch(self):
        adapter = self.setup_adapter()
        task = adapter.task
        result = AttemptSupervisor(task, lambda g, e: stale_candidate(g, e, adapter), pass_verifier).run()
        self.assertEqual((result["attempt_status"], result["budget"]["dispatches"]), ("completed", 0))
        self.assertEqual(result["budget"]["steps"], 2)
        self.assertIn("reobserve", [row["kind"] for row in result["trace"]])

    def test_unknown_candidate_blocks_before_dispatch(self):
        adapter = self.setup_adapter()
        result = AttemptSupervisor(adapter.task,
                                   lambda g, e: rejected_candidate(g, e, adapter, "click", {"element": "9"})).run()
        self.assertEqual((result["stop_reason"], result["budget"]["dispatches"]), ("invalid_action", 0))

    def test_substituted_slot_blocks_before_dispatch(self):
        adapter = self.setup_adapter()
        adapter.backend.nodes[0] = SimpleNamespace(attributes={"data-reflex-id": "name"},
                                                   backend_node_id=15, tag_name="input")
        result = AttemptSupervisor(adapter.task,
                                   lambda g, e: rejected_candidate(g, e, adapter, "type_text",
                                                                      {"field": "0", "value": "other@1"})).run()
        self.assertEqual((result["stop_reason"], result["budget"]["dispatches"]), ("invalid_action", 0))
        self.assertNotIn("other@1", str(result["trace"]))

    def test_policy_revocation_after_selection_prevents_dispatch(self):
        adapter = self.setup_adapter()
        result = AttemptSupervisor(adapter.task, lambda g, e: revoked_after_selection(g, e, adapter),
                                   pass_verifier).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["budget"]["dispatches"]),
                         ("blocked", "policy_denied", 0))
        self.assertEqual(result["policy"]["revision"], 2)

    def test_revoked_operation_is_no_longer_offered(self):
        adapter = self.setup_adapter()
        adapter.permitted = lambda operation: operation != "submit_form"
        obs = adapter.observe()
        self.assertEqual(obs.candidates["elements"], {})
        self.assertEqual(adapter.admit("click", {"element": "0"}), "policy_denied")

    def test_model_sees_page_labels_and_readable_slot_names(self):
        from reflexmesh.adapters.system_one.fixture_browser import slot_descriptions
        adapter = self.setup_adapter()
        obs = adapter.observe()
        self.assertEqual(obs.candidates["elements"], {"0": "button 'Send'"})
        slots = ExecutionTask.from_dict({**sample(), "text_slots": [
            {"id": "name", "version": 1, "value": "a"}, {"id": "name", "version": 2, "value": "b"},
            {"id": "email", "version": 1, "value": "c"}]}).slots
        self.assertEqual(slot_descriptions(slots), {"name@1": "name (version 1)", "name@2": "name (version 2)",
                                                    "email@1": "email"})

    def test_outside_origin_is_rejected(self):
        adapter = self.setup_adapter()
        adapter.backend.url = "https://external.example/form"
        with self.assertRaises(ValueError):
            adapter.observe()


if __name__ == "__main__":
    unittest.main()
