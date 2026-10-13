"""Gate, admission and journal for the generic browser path, against a fake adapter.

No browser is involved: these checks pin down what the runtime does with an adapter's answers
(stale target, revoked permission, unknown effect, cancel, deadline, redaction).
"""

import json
import multiprocessing as mp
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reflexmesh.adapters.system_one.browser_adapter import PreparedBrowserAction, Target
from reflexmesh.contracts.browser import BrowserTask, OPERATION_BITS as BROWSER_BITS
from reflexmesh.contracts.execution import OPERATION_BITS as FIXTURE_BITS, ExecutionTask
from reflexmesh.runtime.browser_run import AttemptTask, BrowserControlledEnvironment, CriteriaVerifier, Journal
from reflexmesh.runtime.runner import AttemptGate, RuntimeStop, SkippedAction
from reflexmesh.text.slots import SlotRegistry
from reflexmesh.verification.constraints import assess_constraints
from test_v05_runtime import sample
from v06_samples import task_dict

CTX = mp.get_context("fork")


class FakeAdapter:
    """What the control layer needs from an adapter; behavior is scripted per test."""

    def __init__(self):
        self.report = {"observation_id": 1, "url": "http://127.0.0.1:8770/orders", "coverage": {"status": "complete"},
                       "text_chars": 10, "options": {}, "option_list": [], "signature": "sig"}
        self.effect_report = None
        self.prepare_errors: list[str | None] = []
        self.results: list = []
        self.executed: list[int] = []
        self.closed = False

    def reset(self, goal):
        pass

    def observe(self):
        self.report = {**self.report, "observation_id": self.report["observation_id"] + 1}
        return SimpleNamespace(text="page", fields={}, candidates={})

    def prepare(self, action, params):
        if self.prepare_errors:
            error = self.prepare_errors.pop(0)
            if error:
                raise RuntimeStop(error)
        operation = {"type_text": "type_text"}.get(action, action)
        risk = "read" if operation in ("scroll", "wait", "switch_tab") else "write"
        target = Target(kind="elements", key="1", operation=operation, role="button", name="Go")
        return PreparedBrowserAction(action, operation, risk, self.report["observation_id"],
                                     f"{self.report['observation_id']}:elements:1", (("element", "1"),), target)

    def execute_prepared(self, prepared, action_id):
        self.executed.append(action_id)
        outcome = self.results.pop(0) if self.results else SimpleNamespace(ok=True)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def state_key(self, prepared):
        return f"{prepared.operation}:{prepared.ref}@{self.report['signature']}"

    def mutation_key(self, prepared):
        return f"{prepared.operation}:{prepared.target.name}@{self.report['url']}"

    # verifier hooks: a scripted probe result per call, and a document id that a reopen renews
    probe = {"state": "unknown", "reason": "no_anchor", "anchors": 0}
    reopened = 0

    def inspect(self, anchor, field_name):
        self.inspected = (anchor, field_name)
        return self.probe

    def document_id(self):
        return f"loader-{self.reopened}"

    def reopen(self):
        self.reopened += 1

    def close(self):
        self.closed = True


class Harness:
    def __init__(self, test, task=None, **limits):
        data = task_dict()
        data["limits"] = {**data["limits"], **limits}
        self.task = task or BrowserTask.from_dict(data)
        self.view = AttemptTask(self.task, "attempt-1")
        registry = SlotRegistry.from_task(self.view, "attempt-1")
        self.gate = AttemptGate(CTX, self.view, time.monotonic(), slot_registry=registry)
        self.journal = Journal(self.task, "attempt-1", None, "script")
        self.adapter = FakeAdapter()
        self.env = BrowserControlledEnvironment(self.adapter, self.gate, self.journal, self.task)
        self.test = test
        self.env.reset("goal")
        self.env.observe()

    def kinds(self):
        return [r["kind"] for r in self.journal.records]


class GateVocabulary(unittest.TestCase):
    def test_gate_takes_its_operation_table_from_the_task(self):
        harness = Harness(self)
        gate = harness.gate
        self.assertIs(gate.bits, BROWSER_BITS)
        self.assertTrue(gate.permitted("click") and gate.permitted("scroll"))
        self.assertFalse(gate.permitted("submit_form"))  # a fixture operation, unknown here
        before = gate.snapshot()
        gate.commit_dispatch("nontext", operation="click")
        after = gate.snapshot()
        self.assertEqual(after["unchecked_commits"], before["unchecked_commits"])
        self.assertTrue(after["committed_operations"] & BROWSER_BITS["click"])

    def test_fixture_tasks_keep_the_fixture_table(self):
        task = ExecutionTask.from_dict(sample())
        gate = AttemptGate(CTX, task, time.monotonic())
        self.assertIs(gate.bits, FIXTURE_BITS)
        self.assertNotEqual(set(FIXTURE_BITS), set(BROWSER_BITS))

    def test_revocation_stops_an_operation_and_bumps_the_revision(self):
        harness = Harness(self)
        revision = harness.gate.revision()
        self.assertGreater(harness.gate.revoke("click"), 0)
        self.assertEqual(harness.gate.revision(), revision + 1)
        with self.assertRaises(RuntimeStop) as caught:
            harness.gate.commit_dispatch("nontext", operation="click")
        self.assertEqual(caught.exception.reason, "policy_denied")


class Admission(unittest.TestCase):
    def test_stale_target_is_never_dispatched(self):
        h = Harness(self)
        dispatches = h.gate.snapshot()["dispatches"]
        h.adapter.prepare_errors = ["stale_target"]
        self.assertIsInstance(h.env.execute("click", {"element": "1"}), SkippedAction)
        self.assertEqual(h.gate.snapshot()["dispatches"], dispatches)
        self.assertEqual(h.adapter.executed, [])
        self.assertEqual(h.kinds()[-3:], ["proposal", "admission_rejected", "reobserve"])

    def test_three_stale_targets_in_a_row_become_a_stale_loop(self):
        h = Harness(self)
        h.adapter.prepare_errors = ["stale_target", "stale_target", "stale_target"]
        h.env.execute("click", {"element": "1"})
        h.env.execute("click", {"element": "1"})
        with self.assertRaises(RuntimeStop) as caught:
            h.env.execute("click", {"element": "1"})
        self.assertEqual(caught.exception.reason, "stale_loop")
        self.assertEqual(h.env.last_stop, "stale_loop")

    def test_an_admitted_action_resets_the_stale_streak(self):
        h = Harness(self)
        h.adapter.prepare_errors = ["stale_target", "stale_target", None, "stale_target", "stale_target"]
        for _ in range(2):
            h.env.execute("click", {"element": "1"})
        h.env.execute("click", {"element": "1"})  # admitted
        for _ in range(2):
            h.env.execute("click", {"element": "1"})  # two stale again: still below the limit

    def test_permission_revoked_after_the_option_was_offered(self):
        h = Harness(self)
        h.gate.revoke("click")
        before = h.gate.snapshot()["dispatches"]
        with self.assertRaises(RuntimeStop) as caught:
            h.env.execute("click", {"element": "1"})
        self.assertEqual(caught.exception.reason, "policy_denied")
        self.assertEqual(h.gate.snapshot()["dispatches"], before)
        self.assertEqual(h.adapter.executed, [])

    def test_policy_changed_since_the_observation_forces_a_new_observation(self):
        h = Harness(self)
        h.gate.revoke("wait")  # click is still allowed, but the candidate set is out of date
        before = h.gate.snapshot()["dispatches"]
        self.assertIsInstance(h.env.execute("click", {"element": "1"}), SkippedAction)
        self.assertEqual(h.gate.snapshot()["dispatches"], before)
        self.assertIn("reobserve", h.kinds())

    def test_adapter_refusals_stop_the_attempt_without_dispatch(self):
        for reason in ("policy_denied", "invalid_action", "adapter_contract_unsupported"):
            h = Harness(self)
            h.adapter.prepare_errors = [reason]
            before = h.gate.snapshot()["dispatches"]
            with self.assertRaises(RuntimeStop):
                h.env.execute("click", {"element": "1"})
            self.assertEqual(h.env.last_stop, reason)
            self.assertEqual(h.gate.snapshot()["dispatches"], before)

    def test_no_dispatch_after_an_accepted_cancel(self):
        h = Harness(self)
        self.assertTrue(h.gate.stop("cancel_requested"))
        before = h.gate.snapshot()["dispatches"]
        with self.assertRaises(RuntimeStop) as caught:
            h.env.execute("click", {"element": "1"})
        self.assertEqual(caught.exception.reason, "cancel_requested")
        self.assertEqual(h.gate.snapshot()["dispatches"], before)
        with self.assertRaises(RuntimeStop):
            h.env.observe()

    def test_deadline_ends_observation_and_dispatch(self):
        h = Harness(self, wall_seconds=0.05)
        time.sleep(0.1)
        with self.assertRaises(RuntimeStop) as caught:
            h.env.observe()
        self.assertEqual(caught.exception.reason, "deadline")
        with self.assertRaises(RuntimeStop):
            h.env.execute("click", {"element": "1"})

    def test_step_and_call_budgets_are_enforced(self):
        h = Harness(self, max_steps=3)
        h.env.observe()
        with self.assertRaises(RuntimeStop) as caught:
            h.env.observe()
        self.assertEqual(caught.exception.reason, "step_limit")
        h2 = Harness(self, max_model_calls=1)
        h2.gate.reserve_call()
        with self.assertRaises(RuntimeStop) as caught:
            h2.gate.reserve_call()
        self.assertEqual(caught.exception.reason, "model_call_limit")


class UnknownEffects(unittest.TestCase):
    def test_a_failed_write_blocks_further_writes_but_not_looking_again(self):
        h = Harness(self)
        h.adapter.results = [SimpleNamespace(ok=False)]
        h.env.execute("click", {"element": "1"})
        with self.assertRaises(RuntimeStop) as caught:
            h.env.execute("click", {"element": "1"})
        self.assertEqual(caught.exception.reason, "effect_unknown")
        self.assertEqual(h.gate.snapshot()["dispatches"], 2)  # bootstrap + the failed click
        self.assertTrue(h.env.execute("scroll", {"direction": "down"}).ok)  # reading cannot change anything
        self.assertEqual(h.gate.snapshot()["dispatches"], 3)

    def test_a_failed_read_does_not_make_later_writes_unknown(self):
        h = Harness(self)
        h.adapter.results = [SimpleNamespace(ok=False)]
        h.env.execute("scroll", {"direction": "down"})
        self.assertTrue(h.env.execute("click", {"element": "1"}).ok)
        h.gate.seal_dispatches()
        rows = {r["id"]: r for r in assess_constraints(
            h.view, h.gate.snapshot(), "attempt-1", dispatches=h.journal.actions, revocations=[],
            driver_outcomes=h.journal.outcomes)}
        self.assertNotEqual(rows["runtime.no_uncertain_repeats"]["status"], "fail")

    def test_waiting_twice_on_an_unchanged_page_is_not_a_repeated_mutation(self):
        h = Harness(self)
        for _ in range(2):
            self.assertTrue(h.env.execute("wait", {}).ok)
        h.env.execute("scroll", {"direction": "down"})
        h.env.execute("scroll", {"direction": "down"})
        h.gate.seal_dispatches()
        rows = {r["id"]: r for r in assess_constraints(
            h.view, h.gate.snapshot(), "attempt-1", dispatches=h.journal.actions, revocations=[],
            driver_outcomes=h.journal.outcomes)}
        self.assertEqual(rows["runtime.no_uncertain_repeats"]["status"], "unknown")

    def test_a_driver_exception_is_an_unknown_effect_not_a_crash(self):
        h = Harness(self)
        h.adapter.results = [ValueError("boom with page text")]
        result = h.env.execute("click", {"element": "1"})
        self.assertFalse(result.ok)
        self.assertEqual(h.journal.outcomes[2], "error")
        self.assertNotIn("page text", json.dumps(h.journal.records))
        with self.assertRaises(RuntimeStop):
            h.env.execute("click", {"element": "1"})
        # The loop may still look at the page.
        h.env.observe()

    def test_read_operations_are_checked_at_the_gate_and_repeat_once_the_page_moved(self):
        h = Harness(self)
        for n in range(3):
            h.adapter.report = {**h.adapter.report, "signature": f"scrolled-{n}"}
            self.assertTrue(h.env.execute("scroll", {"direction": "down"}).ok)
        snapshot = h.gate.snapshot()
        self.assertEqual(snapshot["unchecked_commits"], 0)
        self.assertTrue(snapshot["committed_operations"] & BROWSER_BITS["scroll"])

    def test_constraint_rows_from_the_journal_pass_only_with_evidence(self):
        h = Harness(self)
        h.env.execute("click", {"element": "1"})
        h.gate.seal_dispatches()
        rows = {r["id"]: r for r in assess_constraints(
            h.view, h.gate.snapshot(), "attempt-1", dispatches=h.journal.actions, revocations=[],
            driver_outcomes=h.journal.outcomes)}
        self.assertEqual(rows["runtime.permissions"]["status"], "pass")
        self.assertEqual(rows["runtime.budget"]["status"], "pass")
        self.assertEqual(rows["runtime.dispatch_ordering"]["status"], "pass")
        self.assertEqual(rows["runtime.ownership"]["status"], "unknown")
        self.assertEqual(rows["runtime.no_uncertain_repeats"]["status"], "unknown")

    def test_one_action_is_not_repeated_on_an_unchanged_page(self):
        """V0.6 section 8: refused before dispatch, not found afterwards; the same click once the page moved
        on is a new action."""
        h = Harness(self)
        self.assertTrue(h.env.execute("click", {"element": "1"}).ok)
        skipped = h.env.execute("click", {"element": "1"})  # same target, same page signature
        self.assertIsInstance(skipped, SkippedAction)
        self.assertEqual(h.gate.snapshot()["dispatches"], 2)  # bootstrap + the first click
        self.assertEqual([r["reason"] for r in h.journal.records if r["kind"] == "admission_rejected"],
                         ["repeated_action"])
        self.assertTrue(h.env.execute("scroll", {"direction": "down"}).ok)   # another action is fine
        self.assertIsInstance(h.env.execute("scroll", {"direction": "down"}), SkippedAction)  # reads too
        self.assertTrue(h.env.execute("wait", {}).ok)
        self.assertTrue(h.env.execute("wait", {}).ok)                         # waiting again is allowed
        h.adapter.report = {**h.adapter.report, "signature": "moved", "url": "http://127.0.0.1:8770/orders?q=1"}
        self.assertTrue(h.env.execute("click", {"element": "1"}).ok)
        h.gate.seal_dispatches()
        rows = {r["id"]: r for r in assess_constraints(
            h.view, h.gate.snapshot(), "attempt-1", dispatches=h.journal.actions, revocations=[],
            driver_outcomes=h.journal.outcomes)}
        self.assertNotEqual(rows["runtime.no_uncertain_repeats"]["status"], "fail")

    def test_a_write_is_not_repeated_on_the_same_document_however_the_page_moved(self):
        """V0.6 section 9. A real Jev run (2026-10-13) typed and saved one order note nine times: each
        save added the note to the page, so every repeat looked like a new state. The server effect of a
        write is unknown here, so the same change on the same document is refused before dispatch."""
        h = Harness(self)
        self.assertTrue(h.env.execute("click", {"element": "1"}).ok)                 # Save note
        h.adapter.report = {**h.adapter.report, "signature": "a note is listed now"}  # same URL
        self.assertIsInstance(h.env.execute("click", {"element": "1"}), SkippedAction)
        self.assertEqual(h.gate.snapshot()["dispatches"], 2)                          # bootstrap + one save
        self.assertEqual([r["reason"] for r in h.journal.records if r["kind"] == "admission_rejected"],
                         ["repeated_mutation"])
        self.assertTrue(h.env.execute("scroll", {"direction": "down"}).ok)           # looking is still fine
        h.gate.seal_dispatches()
        rows = {r["id"]: r for r in assess_constraints(
            h.view, h.gate.snapshot(), "attempt-1", dispatches=h.journal.actions, revocations=[],
            driver_outcomes=h.journal.outcomes)}
        self.assertNotEqual(rows["runtime.no_uncertain_repeats"]["status"], "fail")

    def test_the_constraint_names_a_repeated_write_by_document_not_page_state(self):
        """Before 2026-10-13 the assessment keyed rows by page state too and said "no repeated mutation"
        about the nine saves. Two rows with one mutation key and different page states are a repeat."""
        h = Harness(self)
        h.env.execute("click", {"element": "1"})
        h.gate.seal_dispatches()
        row = dict(h.journal.actions[-1])
        again = {**row, "id": row["id"] + 1, "target_id": "same save, another page state"}
        verdict = assess_constraints(h.view, h.gate.snapshot(), "attempt-1",
                                     dispatches=h.journal.actions + [again], revocations=[],
                                     driver_outcomes=h.journal.outcomes)
        rows = {r["id"]: r for r in verdict}
        self.assertEqual(rows["runtime.no_uncertain_repeats"]["status"], "fail")

    def test_three_cycles_without_a_new_state_stop_as_no_progress(self):
        h = Harness(self)                                     # the harness has looked once already
        for _ in range(2):
            h.env.observe()                                   # two cycles without a new state
        with self.assertRaises(RuntimeStop) as caught:
            h.env.observe()                                   # third unchanged cycle
        self.assertEqual(caught.exception.reason, "no_progress")
        h2 = Harness(self)
        for n in range(6):
            h2.adapter.report = {**h2.adapter.report, "signature": f"s{n // 2}"}
            h2.env.observe()                                  # a change every second cycle never stops


class Verification(unittest.TestCase):
    """V0.6 sections 8 and 11 at the control layer: criteria judged on every observation, the reopen of a
    revisit_match is the verifier's own gated navigation, and no page value reaches the journal."""

    NOTE = {"state": "bound", "binding": "named_container", "anchors": 1,
            "values": ["Перезвонить завтра", "Unrelated page text"]}

    def harness(self, task=None):
        h = Harness(self, task=task)
        h.verifier = h.env.verifier = CriteriaVerifier(h.task, "attempt-1", h.journal)
        return h

    def test_the_loop_stops_for_verification_once_every_criterion_holds_on_the_page(self):
        h = self.harness()
        h.env.observe()                                          # nothing bound yet: the loop goes on
        self.assertEqual(h.verifier.rows["note_saved"]["reason"], "no_anchor")
        h.adapter.probe = self.NOTE
        with self.assertRaises(RuntimeStop) as caught:
            h.env.observe()
        self.assertEqual(caught.exception.reason, "criteria_met")
        # A revisit_match is not a pass on the page alone.
        self.assertEqual((h.verifier.rows["note_saved"]["status"], h.verifier.rows["note_saved"]["reason"]),
                         ("unknown", "not_reopened"))
        self.assertEqual(h.adapter.inspected, ("125", "Notes"))

    def test_the_reopen_is_a_gated_navigation_by_the_verifier_and_then_the_criterion_passes(self):
        h = self.harness()
        h.adapter.probe = self.NOTE
        with self.assertRaises(RuntimeStop):
            h.env.observe()
        steps = h.gate.snapshot()["steps"]
        h.verifier.final(h.env)
        row = h.verifier.rows["note_saved"]
        self.assertEqual((row["status"], row["scope"]), ("pass", "ui_after_reopen"))
        self.assertEqual(h.adapter.reopened, 1)
        reopen = h.journal.actions[-1]
        self.assertEqual((reopen["operation"], reopen["producer"], reopen["target_id"]),
                         ("navigate", "verifier", "reopen"))
        self.assertEqual(h.gate.snapshot()["steps"], steps + 1)
        # Page text is not a slot value, so redaction would not catch it: it must never be written at all.
        self.assertNotIn("Unrelated page text", json.dumps(h.journal.records, ensure_ascii=False))

    def test_without_navigate_permission_the_value_on_the_page_stays_unknown(self):
        # Built by hand: the harness's own first navigation needs the permission this task withholds.
        data = task_dict()
        data["permissions"] = [p for p in data["permissions"] if p != "navigate"]
        task = BrowserTask.from_dict(data)
        view = AttemptTask(task, "attempt-1")
        gate = AttemptGate(CTX, view, time.monotonic(), slot_registry=SlotRegistry.from_task(view, "attempt-1"))
        journal = Journal(task, "attempt-1", None, "script")
        adapter = FakeAdapter()
        adapter.report = {**adapter.report, "observation_id": 1}
        env = BrowserControlledEnvironment(adapter, gate, journal, task)
        env.verifier = verifier = CriteriaVerifier(task, "attempt-1", journal)
        adapter.probe = self.NOTE
        verifier.page_check(adapter)
        verifier.final(env)
        self.assertEqual((verifier.rows["note_saved"]["status"], verifier.rows["note_saved"]["reason"]), ("unknown", "not_reopened"))
        self.assertEqual(adapter.reopened, 0)

    def test_a_reopen_that_brings_back_another_value_fails(self):
        h = self.harness()
        h.adapter.probe = self.NOTE
        h.verifier.page_check(h.adapter)
        h.adapter.reopen = lambda: (setattr(h.adapter, "reopened", 1),
                                    setattr(h.adapter, "probe", {**self.NOTE, "values": ["No notes yet."]}))
        h.verifier.final(h.env)
        self.assertEqual((h.verifier.rows["note_saved"]["status"], h.verifier.rows["note_saved"]["reason"]),
                         ("fail", "value_differs"))

    def test_a_secret_slot_is_never_compared(self):
        data = task_dict()
        data["text_slots"][1]["sensitivity"] = "secret"
        h = self.harness(task=BrowserTask.from_dict(data))
        h.adapter.probe = self.NOTE
        h.verifier.page_check(h.adapter)
        self.assertEqual(h.verifier.rows["note_saved"]["reason"], "secret_slot_not_compared")


class EssentialState(unittest.TestCase):
    """Browser Use renumbers every control on every capture; that alone is not a new state."""

    def test_renumbering_and_new_marks_do_not_change_the_essential_text(self):
        from reflexmesh.adapters.system_one.browser_adapter import essential_text

        first = "Orders\n[25]<input id=q />\n[26]<button />\n\tSearch\nSet so far: Order number: '125'."
        again = "Orders\n*[62]<input id=q />\n*[63]<button />\n\tSearch\nSet so far: Order number: '125'."
        self.assertEqual(essential_text(first), essential_text(again))
        self.assertNotEqual(essential_text(first), essential_text(first.replace("'125'", "'126'")))

    def test_an_action_is_named_by_its_target_not_its_index(self):
        from reflexmesh.adapters.system_one.browser_adapter import action_identity

        def click(index, name="Search", oid=1):
            target = Target(kind="elements", key=str(index), operation="click", index=index, role="button", name=name)
            return PreparedBrowserAction("click", "click", "write", oid, f"{oid}:elements:{index}",
                                         (("element", str(index)),), target)

        self.assertEqual(action_identity(click(26)), action_identity(click(92, oid=7)))
        self.assertNotEqual(action_identity(click(26)), action_identity(click(26, name="Save")))
        scroll = PreparedBrowserAction("scroll", "scroll", "read", 1, "scroll:down", (("direction", "down"),))
        self.assertNotEqual(action_identity(scroll), action_identity(
            PreparedBrowserAction("scroll", "scroll", "read", 1, "scroll:up", (("direction", "up"),))))


class JournalRedaction(unittest.TestCase):
    def test_slot_values_do_not_reach_the_trace(self):
        data = task_dict(text_slots=[
            {"id": "order", "version": 1, "value": "125", "sensitivity": "normal"},
            {"id": "note", "version": 1, "value": "Перезвонить завтра", "sensitivity": "normal"},
            {"id": "pw", "version": 1, "value": "hunter2", "sensitivity": "secret"}])
        task = BrowserTask.from_dict(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            journal = Journal(task, "attempt-1", path, "script")
            journal.emit("observed", page="Перезвонить завтра / hunter2 / order 125",
                         options=[{"name": "field hunter2"}])
            journal.close()
            text = path.read_text(encoding="utf-8")
        self.assertNotIn("Перезвонить завтра", text)
        self.assertNotIn("hunter2", text)
        self.assertIn("[slot:note@1]", text)
        self.assertIn("125", text)  # a value this short cannot be told apart from ordinary text

    def test_event_order_is_proposal_then_dispatch_then_return(self):
        h = Harness(self)
        h.env.execute("click", {"element": "1"})
        kinds = h.kinds()
        self.assertLess(kinds.index("proposal", kinds.index("observed")), len(kinds))
        last = kinds[-3:]
        self.assertEqual(last, ["proposal", "dispatch_committed", "driver_returned"])


if __name__ == "__main__":
    unittest.main()
