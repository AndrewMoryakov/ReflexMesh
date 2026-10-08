"""Prepared-command and real adapter producer tests using an in-memory driver.

These are U regression source, not Browser Use/browser acceptance evidence.
"""

import asyncio
import functools
import importlib.util
import inspect
import json
import logging
import multiprocessing as mp
import sys
import time
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser
from reflexmesh.adapters.system_one.text_dispatch import BOUNDARY
from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.runner import AttemptGate, RuntimeStop
from reflexmesh.text.slots import PreparedTextCommand, ResolvedSlot, SlotRegistry
from reflexmesh.tracing.slot_evidence import SlotEvidence
from test_v05_runtime import sample


class RecordingSink:
    """Unit seam only; supervisor/real-ledger positive tests live separately."""

    def __init__(self, expected):
        self.expected = expected
        self.records = []

    def handoff(self, action_id, slot_ref, actual_text, *, boundary):
        matches = type(actual_text) is str and actual_text == self.expected
        self.records.append((action_id, slot_ref, actual_text, boundary, matches))
        return matches


class ActionModel:
    def __init__(self, *, input):
        self.input = SimpleNamespace(**input)

    def model_dump(self, *, exclude_unset):
        return {"input": vars(self.input).copy()}


class Driver:
    def __init__(self, sink):
        self.sink = sink
        self.calls = []
        self.error = None
        self.dom_value = "page-side transformation"
        self.focus = "different-element"

    async def act(self, model, session, sensitive_data=None):
        # Argument passing precedes the receipt; the receipt precedes this
        # coroutine body, which starts only when the returned coroutine is awaited.
        self.calls.append((model, session, sensitive_data,
                           len(getattr(self.sink, "records", ())), model.input.text))
        return SimpleNamespace(error=self.error, extracted_content=model.input.text)


class Backend:
    def __init__(self, task, sink):
        self.url = task.origin + "/form"
        self.nodes = {0: SimpleNamespace(attributes={"data-reflex-id": "name"},
                                         backend_node_id=15, tag_name="input")}
        self._session = self
        self._tools = Driver(sink)
        self.text_values = {"name@1": "mutable backend value must be ignored"}
        self.created_model = None
        self.on_observe = None
        self.executed = []

    def _action_model(self, **kwargs):
        self.created_model = ActionModel(**kwargs)
        return self.created_model

    @staticmethod
    def _run(coro):
        return asyncio.run(coro)

    async def get_selector_map(self):
        return self.nodes

    def observe(self):
        if self.on_observe:
            self.on_observe()
        return SimpleNamespace(fields={"url": self.url},
                               candidates={"elements": {"0": "Name"},
                                           "text_fields": {"0": "Name"}})

    def execute(self, action, params):
        self.executed.append((action, params))
        raise AssertionError("Text must never use backend.execute/text_values")

    def close(self):
        pass


def bound_adapter(task, registry, sink):
    adapter = FixtureBrowser.__new__(FixtureBrowser)
    adapter.task = task
    adapter.backend = Backend(task, sink)
    adapter.targets = {}
    adapter.observation_url = ""
    adapter.unsupported = False
    # Slot-handoff unit seam only. Ownership is deliberately unevidenced;
    # exercise the real slot boundary without inventing a browser claim.
    adapter.bind_ownership = lambda owner: None
    adapter.bind_profile_owner = lambda client: None
    adapter._assert_ownership = lambda **kwargs: None
    adapter.bind_runtime(registry, sink)
    adapter.observe()
    return adapter


def adapter_for(value="sentinel private value"):
    raw = sample()
    raw["text_slots"][0]["value"] = value
    task = ExecutionTask.from_dict(raw)
    registry = SlotRegistry.from_task(task)
    sink = RecordingSink(value)
    adapter = bound_adapter(task, registry, sink)
    return adapter, registry, sink


def execute(adapter, prepared, action_id=1):
    package = ModuleType("systemone_harness")
    package.__path__ = []
    environment = ModuleType("systemone_harness.environment")
    environment.Result = lambda **kwargs: SimpleNamespace(**kwargs)
    with patch.dict(sys.modules, {"systemone_harness": package,
                                  "systemone_harness.environment": environment}):
        return adapter.execute_prepared(prepared, action_id)


class ImmutableSlotBoundary(unittest.TestCase):
    def test_real_ledger_and_real_boundary_producer_with_fake_driver(self):
        adapter, registry, _ = adapter_for()
        task = replace(adapter.task, limits=replace(adapter.task.limits, wall_seconds=None))
        ctx = mp.get_context("fork")
        evidence = SlotEvidence(ctx, task, "boundary-attempt", registry=registry)
        gate = AttemptGate(ctx, task, time.monotonic(), slot_registry=registry, slot_sink=evidence.sink)
        evidence.bind_dispatch_counter(gate.dispatches)
        try:
            adapter = bound_adapter(task, registry, evidence.sink)
            prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
            gate.reserve_step()
            action_id = gate.commit_dispatch("text")
            evidence.sink.commit(action_id, "text", "name@1")
            execute(adapter, prepared, action_id)
            gate.settle(action_id)
            view = evidence.view(gate.seal_dispatches())
            self.assertEqual(view.status, "pass")
            self.assertEqual(len(adapter.backend._tools.calls), 1)
            self.assertTrue(view.evidence_refs)
            self.assertNotIn(registry.resolve("name@1")._value, json.dumps(evidence.export()))
        finally:
            evidence.close()

    def test_real_ledger_requires_actual_handoff_not_prepared_intent(self):
        for forwarded_wrong in (False, True):
            with self.subTest(forwarded_wrong=forwarded_wrong):
                adapter, registry, _ = adapter_for()
                task = replace(adapter.task, limits=replace(adapter.task.limits, wall_seconds=None))
                ctx = mp.get_context("fork")
                evidence = SlotEvidence(ctx, task, "boundary-fault", registry=registry)
                gate = AttemptGate(ctx, task, time.monotonic(), slot_registry=registry, slot_sink=evidence.sink)
                evidence.bind_dispatch_counter(gate.dispatches)
                try:
                    adapter = bound_adapter(task, registry, evidence.sink)
                    prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
                    gate.reserve_step()
                    action_id = gate.commit_dispatch("text")
                    evidence.sink.commit(action_id, "text", "name@1")
                    if forwarded_wrong:
                        prepared = replace(prepared, _text=PreparedTextCommand(0, ResolvedSlot("name@1", "wrong")))
                        execute(adapter, prepared, action_id)
                        self.assertEqual(adapter.backend._tools.calls[0][-1], "wrong")
                    gate.settle(action_id)
                    view = evidence.view(gate.seal_dispatches())
                    self.assertEqual(view.status, "fail" if forwarded_wrong else "unknown")
                finally:
                    evidence.close()

    def test_exact_empty_whitespace_newlines_and_unicode_reach_driver(self):
        for value in ("", "  \t ", "line one\r\nline two\n", "é", "e\u0301", "名字 🧪\u200b",
                      "<secret>name</secret>", "\ud800"):
            with self.subTest(value=value):
                adapter, registry, sink = adapter_for(value)
                prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
                result = execute(adapter, prepared)
                model, session, sensitive, witnesses, actual = adapter.backend._tools.calls[0]
                self.assertTrue(result.ok)
                self.assertIs(model, adapter.backend.created_model)
                self.assertIs(session, adapter.backend._session)
                self.assertIsNone(sensitive)
                self.assertEqual(witnesses, 1)
                self.assertEqual(actual, value)
                self.assertEqual(sink.records, [(1, "name@1", value, BOUNDARY, True)])
                self.assertEqual(adapter.backend.executed, [])

    def test_unknown_reference_version_raw_text_and_extra_arguments_block(self):
        for params in ({"field": "0", "value": "unknown@1"},
                       {"field": "0", "value": "name@2"},
                       {"field": "0", "value": "sentinel private value"},
                       {"field": "0", "value": "name@1", "text": "raw"},
                       {"field": "0", "value": "name@1", "clear": False},
                       {"field": 0, "value": "name@1"}, {"field": "0"}):
            with self.subTest(params=params):
                adapter, registry, sink = adapter_for()
                with self.assertRaises(RuntimeStop) as error:
                    adapter.prepare("type_text", params)
                self.assertEqual(error.exception.reason, "invalid_action")
                self.assertEqual(sink.records, [])
                self.assertEqual(adapter.backend._tools.calls, [])

    def test_input_and_backend_mutations_cannot_change_captured_command(self):
        adapter, registry, sink = adapter_for()
        params = {"field": "0", "value": "name@1"}
        adapter.backend.on_observe = lambda: params.update(field="999", value="name@2", text="raw")
        prepared = adapter.prepare("type_text", params)
        adapter.backend.text_values["name@1"] = "changed after admission"
        adapter.backend.text_values.clear()
        adapter.task = replace(adapter.task, slots=())
        execute(adapter, prepared)
        self.assertEqual(prepared.descriptor(), {"operation": "type_text", "target_id": "name", "slot_ref": "name@1"})
        self.assertEqual(adapter.backend._tools.calls[0][-1], sink.expected)

    def test_registry_resolves_once_and_driver_never_looks_up_again(self):
        adapter, registry, sink = adapter_for()
        original = SlotRegistry.resolve
        calls = []

        def counted(instance, reference):
            calls.append(reference)
            return original(instance, reference)

        with patch.object(SlotRegistry, "resolve", counted):
            prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
        with patch.object(SlotRegistry, "resolve", side_effect=AssertionError("second lookup")):
            execute(adapter, prepared)
        self.assertEqual(calls, ["name@1"])

    def test_private_objects_are_immutable_and_public_views_are_redacted(self):
        adapter, registry, sink = adapter_for()
        resolved = registry.resolve("name@1")
        prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
        for item, name, value in ((registry, "references", ()), (resolved, "_value", "changed"),
                                  (prepared._text, "index", 42), (prepared, "target_id", "email")):
            with self.assertRaises((AttributeError, FrozenInstanceError)):
                setattr(item, name, value)
        with self.assertRaises(TypeError):
            registry._slots["name@1"] = ResolvedSlot("name@1", "changed")
        public = [item.to_dict() for item in (registry, resolved, prepared._text, prepared)]
        rendered = json.dumps(public) + repr((registry, resolved, prepared._text, prepared))
        self.assertNotIn(sink.expected, rendered)
        public[-1]["slot_ref"] = "other@1"
        self.assertEqual(prepared.slot_ref, "name@1")

    def test_runtime_binding_rejects_different_task_revision_run_or_sink(self):
        adapter, registry, sink = adapter_for()
        adapter.bind_runtime(registry, sink)
        for task in (replace(adapter.task, task_id="other"), replace(adapter.task, revision=2),
                     replace(adapter.task, run_id="other-run")):
            with self.assertRaises(RuntimeStop):
                adapter.bind_runtime(SlotRegistry.from_task(task), sink)
        with self.assertRaises(RuntimeStop):
            adapter.bind_runtime(registry, RecordingSink(sink.expected))

    def test_model_field_mutation_blocks_without_claiming_a_handoff(self):
        adapter, registry, sink = adapter_for()
        prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})

        def changed(**kwargs):
            model = ActionModel(**kwargs)
            model.input.text = "changed by model construction"
            return model

        adapter.backend._action_model = changed
        with self.assertRaises(RuntimeStop) as error:
            execute(adapter, prepared)
        self.assertEqual(error.exception.reason, "adapter_contract_unsupported")
        self.assertEqual(sink.records, [])
        self.assertEqual(adapter.backend._tools.calls, [])

    def test_serialized_payload_mutation_blocks_without_claiming_a_handoff(self):
        adapter, registry, sink = adapter_for()
        prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})

        class ChangedSerializer(ActionModel):
            def model_dump(self, *, exclude_unset):
                payload = super().model_dump(exclude_unset=exclude_unset)
                payload["input"]["text"] = "serialized replacement"
                return payload

        adapter.backend._action_model = ChangedSerializer
        with self.assertRaises(RuntimeStop):
            execute(adapter, prepared)
        self.assertEqual(sink.records, [])
        self.assertEqual(adapter.backend._tools.calls, [])

    def test_serializer_cannot_swap_input_object_after_dump(self):
        adapter, registry, sink = adapter_for()
        prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})

        class ReplacedInput(ActionModel):
            def model_dump(self, *, exclude_unset):
                payload = super().model_dump(exclude_unset=exclude_unset)
                self.input = SimpleNamespace(index=0, text="replacement", clear=True)
                return payload

        adapter.backend._action_model = ReplacedInput
        with self.assertRaises(RuntimeStop):
            execute(adapter, prepared)
        self.assertEqual(sink.records, [])
        self.assertEqual(adapter.backend._tools.calls, [])

    def test_actual_wrong_forwarded_value_is_compared_not_prepared_intent(self):
        adapter, registry, sink = adapter_for()
        prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
        # Test-only corruption simulates an adapter fault. The actual wrong text
        # must cross the fake driver boundary for a mismatch witness to be valid.
        wrong = PreparedTextCommand(0, ResolvedSlot("name@1", "actually forwarded wrong text"))
        corrupted = replace(prepared, _text=wrong)
        execute(adapter, corrupted)
        self.assertFalse(sink.records[0][-1])
        self.assertEqual(adapter.backend._tools.calls[0][-1], wrong._value)

    def test_missing_or_unsupported_async_boundary_fails_closed(self):
        for field, replacement in (("_tools", SimpleNamespace(act=lambda *_args, **_kw: None)),
                                   ("_action_model", None), ("_session", None)):
            with self.subTest(field=field):
                adapter, registry, sink = adapter_for()
                prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
                setattr(adapter.backend, field, replacement)
                with self.assertRaises(RuntimeStop) as error:
                    execute(adapter, prepared)
                self.assertEqual(error.exception.reason, "adapter_contract_unsupported")
                self.assertEqual(sink.records, [])

    def test_pinned_sync_decorator_of_async_act_is_supported(self):
        adapter, registry, sink = adapter_for()
        original = adapter.backend._tools.act

        @functools.wraps(original)
        def timing_wrapper(*args, **kwargs):
            return original(*args, **kwargs)

        adapter.backend._tools.act = timing_wrapper
        prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
        execute(adapter, prepared)
        self.assertTrue(sink.records[0][-1])

    def test_wrapper_call_must_return_awaitable_before_receipt(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                adapter, registry, sink = adapter_for()
                original = adapter.backend._tools.act

                @functools.wraps(original)
                def broken_wrapper(*args, **kwargs):
                    if raises:
                        raise ValueError(sink.expected)
                    return None

                adapter.backend._tools.act = broken_wrapper
                prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
                with self.assertRaises(RuntimeStop) as error:
                    execute(adapter, prepared)
                self.assertNotIn(sink.expected, str(error.exception) + repr(error.exception))
                self.assertEqual(sink.records, [])
                self.assertEqual(adapter.backend._tools.calls, [])

    def test_known_upstream_tracing_or_debug_logging_is_unsupported(self):
        async def upstream_style_act(model, session, sensitive_data=None):
            raise AssertionError("private text must not reach an unsafe logger/exporter")

        upstream_style_act.__module__ = "browser_use.tools.service"
        for laminar, level in ((object(), logging.INFO), (None, logging.DEBUG)):
            with self.subTest(level=level):
                adapter, registry, sink = adapter_for()
                adapter.backend._tools.act = upstream_style_act
                prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
                logger = logging.Logger("isolated-boundary-test", level=level)
                with patch.dict(upstream_style_act.__globals__, {"Laminar": laminar, "logger": logger}):
                    with self.assertRaises(RuntimeStop) as error:
                        execute(adapter, prepared)
                self.assertEqual(error.exception.reason, "adapter_contract_unsupported")
                self.assertEqual(sink.records, [])

    @unittest.skipUnless(importlib.util.find_spec("browser_use"), "optional pinned Browser Use not installed")
    def test_optional_real_action_model_and_pinned_wrapper_contract(self):
        from browser_use.tools.service import Tools

        tools = Tools()
        self.assertTrue(inspect.iscoroutinefunction(inspect.unwrap(tools.act)))
        for value in ("", " \t\n", "e\u0301 名字 🧪"):
            adapter, registry, sink = adapter_for(value)
            adapter.backend._action_model = tools.registry.create_action_model()
            prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
            execute(adapter, prepared)
            model = adapter.backend._tools.calls[0][0]
            inspect.signature(tools.act).bind(model, object(), sensitive_data=None)
            self.assertEqual(model.input.text, value)
            self.assertEqual(model.model_dump(exclude_unset=True)["input"]["text"], value)

    @unittest.skipUnless(importlib.util.find_spec("systemone_harness") and
                         importlib.util.find_spec("browser_use"),
                         "optional pinned SystemOneHarness and Browser Use not installed")
    def test_optional_real_soh_async_bridge_with_real_action_model(self):
        """Exercise pinned SOH's thread/loop bridge without starting any browser.

        Admission still uses the in-memory fixture. The actual SOH _run bridge,
        Browser Use action model and SOH Result are real; session/driver are fake.
        This does not establish Browser Use action execution or DOM conformance.
        """
        from browser_use.tools.service import Tools
        from systemone_harness.envs.browser import BrowserEnvironment

        adapter, registry, sink = adapter_for(" \t\n名字 e\u0301 🧪 ")
        prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
        # The reviewed constructor starts only its event-loop thread. Browser
        # launch is confined to _start(), which this test must never call.
        backend = BrowserEnvironment(headless=True, text_values={}, start_url=None)
        closed = []

        async def kill_fake_session():
            closed.append(True)

        async def forbidden_start():
            raise AssertionError("This deterministic test cannot start a browser")

        try:
            backend._session = SimpleNamespace(kill=kill_fake_session)
            backend._tools = Driver(sink)
            backend._action_model = Tools().registry.create_action_model()
            backend._start = forbidden_start
            adapter.backend = backend
            result = adapter.execute_prepared(prepared, 1)
            self.assertTrue(result.ok)
            self.assertEqual(backend._tools.calls[0][-1], registry.resolve("name@1")._value)
            self.assertEqual(backend._tools.calls[0][3], 1)
            self.assertTrue(sink.records[0][-1])
        finally:
            # SOH close() runs fake-session kill on its own loop, stops that
            # loop and joins the thread. Close the stopped test-owned loop too.
            backend.close()
            if not backend._thread.is_alive():
                backend._loop.close()
        self.assertFalse(backend._thread.is_alive())
        self.assertEqual(closed, [True])

    def test_dom_value_and_focus_do_not_determine_payload_witness(self):
        adapter, registry, sink = adapter_for()
        prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
        execute(adapter, prepared)
        self.assertTrue(sink.records[0][-1])
        self.assertNotEqual(adapter.backend._tools.dom_value, sink.expected)
        self.assertEqual(adapter.backend._tools.focus, "different-element")

    def test_driver_result_content_and_errors_are_not_relayed(self):
        adapter, registry, sink = adapter_for()
        prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
        adapter.backend._tools.error = sink.expected
        result = execute(adapter, prepared)
        self.assertFalse(result.ok)
        self.assertNotIn(sink.expected, result.text)

    def test_driver_exception_is_sanitized_after_actual_handoff(self):
        adapter, registry, sink = adapter_for()
        prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})

        async def broken_driver(model, session, sensitive_data=None):
            raise ValueError("Driver echoed " + model.input.text)

        adapter.backend._tools.act = broken_driver
        with self.assertRaises(RuntimeStop) as error:
            execute(adapter, prepared)
        self.assertEqual(error.exception.reason, "effect_unknown")
        self.assertNotIn(sink.expected, str(error.exception) + repr(error.exception))
        self.assertTrue(error.exception.__suppress_context__)
        self.assertEqual(len(sink.records), 1)
        self.assertTrue(sink.records[0][-1])

    def test_model_validation_and_result_properties_cannot_leak_errors(self):
        for stage in ("model", "result"):
            with self.subTest(stage=stage):
                adapter, registry, sink = adapter_for()
                prepared = adapter.prepare("type_text", {"field": "0", "value": "name@1"})
                if stage == "model":
                    def broken_model(**kwargs):
                        raise ValueError(kwargs["input"]["text"])
                    adapter.backend._action_model = broken_model
                else:
                    class BrokenResult:
                        @property
                        def error(self):
                            raise ValueError(sink.expected)

                    async def broken_result(model, session, sensitive_data=None):
                        return BrokenResult()
                    adapter.backend._tools.act = broken_result
                with self.assertRaises(RuntimeStop) as error:
                    execute(adapter, prepared)
                self.assertNotIn(sink.expected, str(error.exception) + repr(error.exception))
                self.assertEqual(len(sink.records), 0 if stage == "model" else 1)

    def test_legacy_text_execute_path_is_closed(self):
        adapter, registry, sink = adapter_for()
        with self.assertRaises(RuntimeStop):
            adapter.execute("type_text", {"field": "0", "value": "name@1"})
        self.assertEqual(adapter.backend.executed, [])


if __name__ == "__main__":
    unittest.main()
