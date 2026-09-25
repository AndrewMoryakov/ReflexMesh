"""Fixture target identity checks without claiming a real Browser Use run."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser
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


class FakeBackend:
    def __init__(self, url):
        self.url = url
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


class AdapterContract(unittest.TestCase):
    def setup_adapter(self):
        task = ExecutionTask.from_dict(sample())
        adapter = FixtureBrowser.__new__(FixtureBrowser)
        adapter.task = task
        adapter.backend = FakeBackend(task.origin + "/form")
        adapter.targets = {}
        adapter.unsupported = False
        adapter.observation_url = ""
        return adapter

    def test_filtered_candidate_and_node_replacement(self):
        adapter = self.setup_adapter()
        obs = adapter.observe()
        self.assertEqual(set(obs.candidates["elements"]), {"0"})
        self.assertEqual(adapter.admit("click", {"element": "0"}), None)
        adapter.backend.nodes[0].backend_node_id = 16
        self.assertEqual(adapter.admit("click", {"element": "0"}), "stale_target")

    def test_missing_identity_fails_closed(self):
        adapter = self.setup_adapter()
        adapter.backend.nodes[0].backend_node_id = None
        with self.assertRaises(RuntimeStop) as error:
            adapter.observe()
        self.assertEqual(error.exception.reason, "adapter_contract_unsupported")

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

    def test_outside_origin_is_rejected(self):
        adapter = self.setup_adapter()
        adapter.backend.url = "https://external.example/form"
        with self.assertRaises(ValueError):
            adapter.observe()
