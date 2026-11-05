"""M3: handoff and continuation in the same browser session (V0.6 sections 10 and 14), on a real Chromium.

The deterministic label script stands in for Jev and the test itself for the calling LLM; what is checked is the
runtime: the same browser session across attempts, a new attempt and owner epoch, one shared budget, a one-time
token with a lease, and an old owner that can no longer act.
"""

import contextlib
import copy
import importlib.util
import io
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_v06_browser_real import AVAILABLE, CHROME, ROOT, SANDBOX, SITE, BrowserCase  # noqa: E402

from reflexmesh.contracts.browser import BrowserTask  # noqa: E402
from reflexmesh.runtime.browser_core import BrowserCore  # noqa: E402
from reflexmesh.runtime.runner import RuntimeStop  # noqa: E402

TO_ORDER = [{"action": "type_text", "field": "Order number", "value": "^order@1"},
            {"action": "click", "element": "button 'Search'"}, {"action": "click", "element": "Заказ №125"}]
SAVE = [{"action": "type_text", "field": "textbox 'Note'", "value": "^note@1"}, {"action": "click", "element": "Save note"},
        {"action": "finish"}]


@unittest.skipUnless(AVAILABLE, "set REFLEXMESH_CHROME and install the pinned Browser Use + SystemOneHarness")
class SameSession(BrowserCase):
    def setUp(self):
        from reflexmesh.verification.checks import OrderDeskCheck

        super().setUp()
        self.checks = {"desk": OrderDeskCheck("desk", self.origin)}

    def task(self, *, order="125", without_order=False, revision=1, **limits):
        data = json.loads((ROOT / "experiments/v06/tasks/order-note-verified.json").read_text("utf-8"))
        data.update(start_url=self.origin + "/orders", allowed_origins=[self.origin], revision=revision)
        data["criteria"][1]["args"]["check_id"] = "desk"
        data["limits"].update(limits)
        for slot in data["text_slots"]:
            if slot["id"] == "order":
                slot["value"] = order
        if without_order:
            data["text_slots"] = [s for s in data["text_slots"] if s["id"] != "order"]
        return data

    def core(self, scripts, lease=60.0):
        from reflexmesh.adapters.system_one.decision_providers import LabelScriptProvider

        steps = iter(scripts)
        core = BrowserCore(chrome=CHROME, provider_factory=lambda: (LabelScriptProvider(next(steps)), "script:label", None),
                           action_provider="script", out_dir=Path(self.out.name) / "chain", sandbox=SANDBOX,
                           checks=self.checks, lease_seconds=lease)
        self.addCleanup(core.close)
        return core

    def attempt_dir(self, result, n):
        return Path(self.out.name) / "chain" / f"chain-{result['chain_id']}" / f"attempt-{n}"

    @staticmethod
    def resume_token(result):
        cont = result["handoff"]["continuation"]
        return {"token": cont["token"], "parent_attempt_id": result["attempt_id"]}

    def test_a_missing_value_is_supplied_by_a_new_revision_in_the_same_session(self):
        """The M3 condition: a missing value is resolved without restarting the browser or resetting limits."""
        rev1 = self.task(without_order=True)
        rev2 = self.task(revision=2)
        core = self.core([[{"action": "escalate"}], TO_ORDER + SAVE])
        first = core.execute(BrowserTask.from_dict(rev1))
        self.assertEqual((first["attempt_status"], first["stop_reason"], first["handoff"]["reason"]),
                         ("incomplete", "needs_handoff", "escalation_requested"))
        self.assertEqual(first["cleanup"]["status"], "retained")
        chain = core.chains[first["chain_id"]]
        browser = dict(chain.session.session)
        old_run = chain.runs[0]
        # F07 while the session is still open: the old owner's options are still in the shared adapter, admission
        # would pass, and the gate refuses because that attempt is terminal. Nothing reaches the site.
        pages = len([r for r in SITE.REQUESTS if not r["path"].startswith("/__")])
        key = next(k for (kind, k) in old_run.adapter._table if kind == "elements")
        with self.assertRaises(RuntimeStop) as refused:
            old_run.controlled.execute("click", {"element": key})
        self.assertEqual(refused.exception.reason, "terminal")
        self.assertEqual(len([r for r in SITE.REQUESTS if not r["path"].startswith("/__")]), pages)
        second = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev", "task": rev2})
        self.assertEqual((second["attempt_status"], second["stop_reason"]), ("completed", "verified"))
        # Same session, new attempt and owner epoch, linked to the first.
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(chain.runs[1].adapter.session, browser)          # the same Chrome process, not a new one
        self.assertNotEqual(second["attempt_id"], first["attempt_id"])
        self.assertEqual((second["parent_attempt_id"], first["owner_epoch"], second["owner_epoch"]),
                         (first["attempt_id"], 0, 1))
        self.assertNotIn("navigate", [a["operation"] for a in second["actions"] if a["producer"] != "verifier"])
        # One budget: the chain's steps continue, they do not start again.
        self.assertEqual(second["chain_budget"]["used"]["steps"], first["budget"]["steps"] + second["budget"]["steps"])
        self.assertEqual(second["budget"]["limits"]["max_steps"], 64 - first["budget"]["steps"])
        # The first attempt keeps its status, its owner cannot act, its token is spent.
        stored = json.loads((self.attempt_dir(first, 1) / "result.json").read_text())
        self.assertEqual(stored["attempt_status"], "incomplete")
        self.assertEqual(stored["handoff"]["continuation"]["token"], "[withheld]")   # never on disk
        self.assertEqual(core.resume({**self.resume_token(first), "operation": "start", "mode": "jev"})["error"]["code"],
                         "token_invalid")
        self.assertEqual(SITE.NOTES, {"125": ["Перезвонить завтра"]})
        self.assertEqual(SITE.CLAIM, {})

    def wizard_task(self, *, without_note=False, revision=1):
        data = self.task(revision=revision)
        data.update(json.loads((ROOT / "experiments/v06/tasks/order-note-wizard.json").read_text("utf-8")),
                    revision=revision, start_url=self.origin + "/orders/125/wizard/1", allowed_origins=[self.origin])
        data["criteria"][1]["args"]["check_id"] = "desk"
        if without_note:
            data["text_slots"] = [s for s in data["text_slots"] if s["id"] != "note"]
            data["criteria"] = [c for c in data["criteria"] if c["id"] != "note_saved"]
            data["criteria"][0]["args"]["expected_slot_refs"] = ["team@1"]
        return data

    def test_restart_begins_again_from_start_url_in_the_same_browser(self):
        """`restart: true`: a first navigation through the gate, in the same session and the same process."""
        core = self.core([TO_ORDER[:2] + [{"action": "escalate"}], TO_ORDER + SAVE])
        first = core.execute(BrowserTask.from_dict(self.task()))
        chain = core.chains[first["chain_id"]]
        pid = chain.session.session["pid"]
        self.assertTrue(pid)
        done = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev", "restart": True,
                            "task": self.task(revision=2)})
        self.assertEqual((done["attempt_status"], done["stop_reason"]), ("completed", "verified"))
        self.assertEqual(done["session_id"], first["session_id"])
        rows = {r["id"]: r for r in done["verification"]}
        self.assertEqual(rows["runtime.ownership"]["status"], "pass")
        trace = [json.loads(line) for line in (self.attempt_dir(first, 2) / "trace.jsonl").read_text("utf-8").splitlines()]
        restarted = next(r for r in trace if r["kind"] == "session_restarted")
        self.assertEqual(restarted["browser_pid"], pid)
        self.assertEqual(done["actions"][0]["target_id"], "start_url")    # counted and journalled like any dispatch
        self.assertEqual(done["budget"]["steps"], 8)
        self.assertEqual(SITE.NOTES, {"125": ["Перезвонить завтра"]})

    def test_restart_lets_the_same_value_be_typed_again_on_the_fresh_page(self):
        """The same slot, the same field, the same search: without forgetting the page's state all are repeats."""
        core = self.core([TO_ORDER + SAVE[:1] + [{"action": "escalate"}], TO_ORDER + SAVE])
        first = core.execute(BrowserTask.from_dict(self.task()))
        self.assertEqual(first["handoff"]["reason"], "escalation_requested")
        done = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev", "restart": True})
        self.assertEqual((done["attempt_status"], done["stop_reason"]), ("completed", "verified"))
        self.assertEqual(SITE.NOTES, {"125": ["Перезвонить завтра"]})

    def test_without_restart_the_same_value_stays_a_repeat(self):
        """The guard is unchanged for an ordinary continuation: this is the failing half of the test above."""
        core = self.core([TO_ORDER[:1] + [{"action": "escalate"}], TO_ORDER[:1] + TO_ORDER[:1] + TO_ORDER[:1]])
        first = core.execute(BrowserTask.from_dict(self.task()))
        second = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev"})
        self.assertEqual(second["attempt_status"], "incomplete")
        trace = [json.loads(line) for line in (self.attempt_dir(first, 2) / "trace.jsonl").read_text("utf-8").splitlines()]
        self.assertIn("repeated_mutation", [r.get("reason") for r in trace if r["kind"] == "admission_rejected"])

    def test_restart_follows_the_start_url_of_the_new_revision(self):
        core = self.core([[{"action": "escalate"}], [{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(self.task()))
        revised = self.task(revision=2)
        revised["start_url"] = self.origin + "/orders/125"
        second = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev", "restart": True, "task": revised})
        self.assertEqual(second["handoff"]["page"], self.origin + "/orders/125")
        self.assertEqual(second["actions"][0]["destination"], self.origin + "/orders/125")

    def test_restart_in_llm_assistance_shows_the_fresh_page(self):
        core = self.core([TO_ORDER[:2] + [{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(self.task()))
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm", "restart": True})
        self.assertEqual(progress["schema_version"], "browser-progress/0.1")
        self.assertEqual(progress["observation"]["page"], self.origin + "/orders")
        self.assertNotIn("Заказ №125", progress["observation"].get("text") or "")
        self.assertEqual(progress["assistance_steps_remaining"], 5)
        core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"], "operation": "stop"})

    def test_restart_needs_the_navigate_permission(self):
        """It is a dispatch like any other: without the permission nothing is navigated."""
        core = self.core([TO_ORDER[:2] + [{"action": "escalate"}], [{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(self.task()))
        revised = self.task(revision=2)
        revised["permissions"] = [p for p in revised["permissions"] if p != "navigate"]
        second = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev", "restart": True, "task": revised})
        self.assertEqual((second["attempt_status"], second["stop_reason"]), ("blocked", "policy_denied"))
        self.assertEqual(second["actions"], [])

    def test_restart_is_refused_unless_it_is_a_boolean_on_start(self):
        """Refused before the token is spent: the continuation still works afterwards."""
        core = self.core([[{"action": "escalate"}], [{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(self.task()))
        refused = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev", "restart": "yes"})
        self.assertEqual(refused["error"]["code"], "invalid_continuation")
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm"})
        self.assertEqual(progress["schema_version"], "browser-progress/0.1")
        step = core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"],
                            "operation": "stop", "restart": True})
        self.assertEqual(step["error"]["code"], "invalid_continuation")
        core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"], "operation": "stop"})

    WIZARD_STEP_1 = [{"action": "type_text", "field": "Team", "value": "^team@1"}, {"action": "click", "element": "Next"}]
    WIZARD_REST = [{"action": "type_text", "field": "textbox 'Note'", "value": "^note@1"},
                   {"action": "click", "element": "Next"}, {"action": "click", "element": "Save note"}, {"action": "finish"}]

    def test_a_field_no_slot_may_go_into_is_not_offered_and_the_caller_is_told(self):
        """The note's value is missing: the Note field is no question for the model, and the handoff names it."""
        core = self.core([self.WIZARD_STEP_1 + [{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(self.wizard_task(without_note=True)))
        self.assertEqual(first["handoff"]["page"], self.origin + "/orders/125/wizard/2")
        self.assertEqual(first["handoff"]["fields_without_slot"], ["Note"])
        self.assertNotIn("type_text", [o["operation"] for o in first["handoff"]["available_actions"]])

    def test_a_slot_chosen_for_a_field_it_does_not_fit_is_refused_before_anything_is_sent(self):
        """Section 7: the pair is checked in code after the choice. The assisted path reaches the gate directly."""
        core = self.core([self.WIZARD_STEP_1 + [{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(self.wizard_task(without_note=True)))
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm", "task": self.wizard_task(revision=2)})
        note = next(o for o in progress["action_options"] if o["operation"] == "type_text")
        self.assertEqual((note["name"], note["slot_refs"]), ("Note", ["note@1"]))
        refused = core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"], "operation": "step",
                               "proposal": {"observation_id": progress["observation"]["observation_id"], "operation": "type_text",
                                            "option_ref": note["ref"], "slot_ref": "team@1"}})
        self.assertEqual(refused["last_proposal"], {"accepted": True, "dispatched": False, "rejected": "slot_field_mismatch"})
        self.assertEqual(refused["assistance_steps_remaining"], 5)
        typed = core.resume({"token": refused["continuation_token"], "parent_attempt_id": refused["attempt_id"], "operation": "step",
                             "proposal": {"observation_id": refused["observation"]["observation_id"], "operation": "type_text",
                                          "option_ref": next(o["ref"] for o in refused["action_options"] if o["operation"] == "type_text"),
                                          "slot_ref": "note@1"}})
        self.assertEqual(typed["last_proposal"], {"accepted": True, "dispatched": True})
        core.resume({"token": typed["continuation_token"], "parent_attempt_id": typed["attempt_id"], "operation": "stop"})

    def test_a_revision_cannot_change_where_a_kept_slot_may_go(self):
        core = self.core([[{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(self.task()))
        revised = self.task(revision=2)
        next(s for s in revised["text_slots"] if s["id"] == "note")["fields"] = ["Order number"]
        refused = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev", "task": revised})
        self.assertEqual((refused["error"]["code"], refused["error"]["reason"]), ("revision_refused", "slot_reference_redirected"))

    def test_multistep_form_continues_where_it_stopped_without_restart(self):
        """The missing note arrives at step 2. An ordinary continuation keeps the progress and finishes."""
        core = self.core([self.WIZARD_STEP_1 + [{"action": "escalate"}], self.WIZARD_REST])
        first = core.execute(BrowserTask.from_dict(self.wizard_task(without_note=True)))
        self.assertEqual(first["handoff"]["page"], self.origin + "/orders/125/wizard/2")
        done = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev",
                            "task": self.wizard_task(revision=2)})
        self.assertEqual((done["attempt_status"], done["stop_reason"]), ("completed", "verified"))
        self.assertEqual(SITE.NOTES, {"125": ["Перезвонить завтра"]})
        self.assertEqual([r["path"] for r in self.posts()].count("/orders/125/wizard/1"), 1)

    def test_multistep_form_after_restart_is_stopped_by_the_guard_not_resubmitted(self):
        """The cost of a restart: the page is back at step 1 and its submitted Next is remembered, so the attempt
        stops at a handoff. No step is sent twice and no note appears; it does not claim anything it did not do."""
        next_step = self.WIZARD_STEP_1[1]
        core = self.core([self.WIZARD_STEP_1 + [{"action": "escalate"}], self.WIZARD_STEP_1 + [next_step] * 4])
        first = core.execute(BrowserTask.from_dict(self.wizard_task(without_note=True)))
        second = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev", "restart": True,
                              "task": self.wizard_task(revision=2)})
        self.assertEqual((second["attempt_status"], second["stop_reason"], second["handoff"]["reason"]),
                         ("incomplete", "needs_handoff", "no_progress"))
        self.assertEqual(second["handoff"]["page"], self.origin + "/orders/125/wizard/1")
        self.assertEqual(SITE.NOTES, {})
        self.assertEqual([r["path"] for r in self.posts()].count("/orders/125/wizard/1"), 1)
        trace = [json.loads(line) for line in (self.attempt_dir(first, 2) / "trace.jsonl").read_text("utf-8").splitlines()]
        self.assertIn("repeated_mutation", [r.get("reason") for r in trace if r["kind"] == "admission_rejected"])

    def assisted(self, script):
        """The chain demo's helper, from where the script stopped; the demo's CLI calls replaced by the core."""
        spec = importlib.util.spec_from_file_location("chain_demo", ROOT / "experiments/v06/chain_demo.py")
        demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demo)
        core = self.core([script, [{"action": "escalate"}]])       # the second answers a return to Jev
        first = core.execute(BrowserTask.from_dict(self.task()))
        self.assertEqual((first["attempt_status"], first["handoff"]["reason"]), ("incomplete", "escalation_requested"))
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm"})
        with contextlib.redirect_stdout(io.StringIO()):
            return demo.assist(lambda payload: (0, core.resume(payload)), progress), core, first

    def assert_helper_completed(self, done, core, first):
        self.assertEqual((done["attempt_status"], done["stop_reason"]), ("completed", "verified"))
        self.assertEqual(SITE.NOTES, {"125": ["Перезвонить завтра"]})
        self.assertIn("llm", [a["producer"] for a in core.chains[first["chain_id"]].actions])

    def test_the_demo_helper_continues_from_the_found_order_with_its_link_name_masked(self):
        """Jev stopped on the results page, the case a helper that knows only "Save note" could not help with."""
        done, core, first = self.assisted(TO_ORDER[:2] + [{"action": "escalate"}])
        self.assert_helper_completed(done, core, first)

    def test_the_demo_helper_continues_from_the_search_form(self):
        """Jev typed nothing: the five assisted steps cover the whole path."""
        done, core, first = self.assisted([{"action": "escalate"}])
        self.assert_helper_completed(done, core, first)
        steps = [a["operation"] for a in core.chains[first["chain_id"]].actions if a["producer"] == "llm"]
        self.assertEqual(steps, ["type_text", "click", "click", "type_text", "click"])

    def test_the_demo_helper_hands_back_when_no_step_fits(self):
        """On a page it does not know, it takes no step and spends nothing but the return."""
        done, core, first = self.assisted([{"action": "click", "element": "Dashboard"}, {"action": "escalate"}])
        self.assertEqual(done["attempt_status"], "incomplete")
        self.assertEqual(SITE.NOTES, {})
        self.assertNotIn("llm", [a["producer"] for a in core.chains[first["chain_id"]].actions])

    def test_a_final_probe_reads_the_live_page_and_leaves_no_trace_in_the_result(self):
        """The M5 oracle looks after the attempt ended, before the browser closes; the attempt never knows."""
        spec = importlib.util.spec_from_file_location("m5_runner", ROOT / "experiments/v06/m5/runner.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        steps = TO_ORDER + SAVE
        task = BrowserTask.from_dict(self.task())
        plain = self.core([list(steps)])
        without = plain.execute(task)
        plain.close()
        SITE.NOTES.clear()
        states = []
        core = self.core([list(steps)])
        core.final_probe = lambda adapter: states.append(runner.read_final_state(adapter))
        withit = core.execute(task)
        self.assertEqual((withit["attempt_status"], withit["stop_reason"]), (without["attempt_status"], without["stop_reason"]))
        self.assertEqual(len(states), 1)
        self.assertRegex(states[0]["url"], r"/orders/125$")
        self.assertIn("Перезвонить завтра", states[0]["text"])
        self.assertEqual(sorted(withit), sorted(without))
        self.assertEqual([a["operation"] for a in withit["actions"]], [a["operation"] for a in without["actions"]])

    def test_the_m5_assistant_takes_steps_through_the_gate_and_is_metered(self):
        """J1: the LLM's replies are scripted here, the path through the runtime is real."""
        spec = importlib.util.spec_from_file_location("m5_runner", ROOT / "experiments/v06/m5/runner.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)

        class Client:
            def __init__(self):
                self.script = [lambda o: {"option_ref": next(x["ref"] for x in o if str(x.get("name")).startswith(("Заказ", "[slot:record")))},
                               lambda o: {"option_ref": next(x["ref"] for x in o if x["operation"] == "type_text" and x["name"] == "Note"), "slot_ref": "team@1"},
                               lambda o: {"option_ref": next(x["ref"] for x in o if x["operation"] == "type_text" and x["name"] == "Note"), "slot_ref": "note@1"},
                               lambda o: {"option_ref": next(x["ref"] for x in o if x.get("name") == "Save note")}]

            def ask(self, system, content):
                return self.script.pop(0)(content["action_options"]), {"input_tokens": 10, "output_tokens": 2, "cost": 0.5}, "m", "r", 1

        core = self.core([TO_ORDER[:2] + [{"action": "escalate"}], [{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(self.task()))
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm"})
        assistant = runner.Assistant(Client(), "goal")
        done = assistant.help(core, progress)
        self.assertEqual((done["attempt_status"], done["stop_reason"]), ("completed", "verified"))
        self.assertEqual(SITE.NOTES, {"125": ["Перезвонить завтра"]})
        self.assertEqual((assistant.calls, assistant.steps, assistant.input_tokens, assistant.cost), (4, 3, 40, 2.0))

    def test_ambiguity_is_resolved_by_one_assisted_step_then_jev_continues(self):
        """LLM assistance: one proposal from the fresh options, through the same gate; then return to Jev."""
        task = self.task(order="12")                      # "12" matches orders 124, 125 and 126
        core = self.core([TO_ORDER[:2] + [{"action": "escalate"}], SAVE])
        first = core.execute(BrowserTask.from_dict(task))
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm"})
        self.assertEqual((progress["schema_version"], progress["phase"]), ("browser-progress/0.1", "awaiting_proposal"))
        self.assertEqual(progress["assistance_steps_remaining"], 5)
        orders = [o["name"] for o in progress["action_options"] if str(o.get("name", "")).startswith("Заказ №")]
        self.assertEqual(sorted(orders), ["Заказ №124", "Заказ №125", "Заказ №126"])   # the ambiguity
        pick = next(o for o in progress["action_options"] if o.get("name") == "Заказ №125")
        stale = core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"],
                             "operation": "step", "proposal": {"observation_id": -1, "operation": "click",
                                                               "option_ref": pick["ref"]}})
        self.assertEqual(stale["last_proposal"], {"rejected": "stale_observation"})   # refused, nothing spent
        self.assertEqual(stale["assistance_steps_remaining"], 5)
        moved = core.resume({"token": stale["continuation_token"], "parent_attempt_id": stale["attempt_id"],
                             "operation": "step", "proposal": {"observation_id": stale["observation"]["observation_id"],
                                                               "operation": "click", "option_ref": pick["ref"]}})
        self.assertEqual(moved["observation"]["page"], self.origin + "/orders/125")
        self.assertEqual(moved["assistance_steps_remaining"], 4)
        done = core.resume({"token": moved["continuation_token"], "parent_attempt_id": moved["attempt_id"],
                            "operation": "return_to_jev"})
        self.assertEqual((done["attempt_status"], done["stop_reason"]), ("completed", "verified"))
        self.assertEqual(done["session_id"], first["session_id"])
        chain = core.chains[first["chain_id"]]
        producers = [a["producer"] for a in chain.actions]
        self.assertIn("llm", producers)
        assisted = json.loads((self.attempt_dir(first, 2) / "result.json").read_text())
        self.assertEqual((assisted["attempt_status"], assisted["handoff"]["reason"]),
                         ("incomplete", "assistance_returned"))
        self.assertEqual(chain.handoffs_used, 2)                   # start(llm) and the return to Jev
        self.assertEqual(SITE.NOTES, {"125": ["Перезвонить завтра"]})

    def test_a_resumed_attempt_stops_at_the_chain_limit_not_a_fresh_one(self):
        task = self.task(without_order=True, max_steps=6)
        # Each click opens another page, so neither repeat guard stops it: only the budget can.
        loop = [{"action": "click", "element": "Dashboard"}, {"action": "click", "element": "link 'Orders'"}] * 10
        core = self.core([[{"action": "escalate"}], loop])
        first = core.execute(BrowserTask.from_dict(task))
        second = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev"})
        self.assertEqual(second["chain_budget"]["used"]["steps"], 6)
        self.assertEqual(second["stop_reason"], "step_limit")
        self.assertLessEqual(second["budget"]["steps"], 6 - first["budget"]["steps"])

    def test_an_expired_lease_closes_the_browser_and_kills_the_token(self):
        import psutil

        core = self.core([[{"action": "escalate"}]], lease=1.0)
        first = core.execute(BrowserTask.from_dict(self.task(without_order=True)))
        chain = core.chains[first["chain_id"]]
        pid = chain.session.session["pid"]
        self.assertTrue(psutil.pid_exists(pid))
        time.sleep(2.5)
        self.assertTrue(chain.closed)
        self.assertFalse(psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE)
        refused = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev"})
        self.assertIn(refused["error"]["code"], ("chain_closed", "session_closed", "token_expired"))
        self.assertEqual(SITE.CLAIM, {})

    def test_a_refused_continuation_closes_the_session_it_cannot_use(self):
        import psutil

        from reflexmesh.adapters.system_one.decision_providers import LabelScriptProvider, ProviderUnavailable

        calls = []

        def factory():
            calls.append(1)
            if len(calls) > 1:
                raise ProviderUnavailable("no key")
            return LabelScriptProvider([{"action": "escalate"}]), "script:label", None

        core = self.core([])
        core.provider_factory = factory
        first = core.execute(BrowserTask.from_dict(self.task(without_order=True)))
        chain = core.chains[first["chain_id"]]
        pid = chain.session.session["pid"]
        refused = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev"})
        self.assertEqual((refused["attempt_status"], refused["stop_reason"]), ("blocked", "executor_unavailable"))
        self.assertTrue(chain.closed)
        time.sleep(0.5)
        self.assertFalse(psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE)
        self.assertEqual(SITE.CLAIM, {})

    def test_a_second_controller_at_a_handoff_is_a_violation_not_a_transfer(self):
        import subprocess

        holder = []
        core = self.core([])

        from reflexmesh.adapters.system_one.decision_providers import LabelScriptProvider

        class Attach(LabelScriptProvider):
            def decide(self, state, questions):
                if self.calls == 1 and not holder:
                    port = core.chains[next(iter(core.chains))].runs[0].adapter.session["port"]
                    holder.append(subprocess.Popen([sys.executable, "-c",
                        f"import socket,time; s=socket.create_connection(('127.0.0.1',{port})); time.sleep(60)"]))
                    time.sleep(0.5)
                return super().decide(state, questions)

        core.provider_factory = lambda: (Attach(TO_ORDER[:2] + [{"action": "escalate"}]), "script:label", None)
        try:
            first = core.execute(BrowserTask.from_dict(self.task()))
        finally:
            for process in holder:
                process.kill()
                process.wait()
        self.assertEqual((first["attempt_status"], first["stop_reason"]), ("failed", "constraint_violated"))
        self.assertNotEqual(first["cleanup"]["status"], "retained")
        self.assertTrue(core.chains[first["chain_id"]].closed)

    def test_a_lease_that_runs_out_while_waiting_for_a_proposal_ends_the_assistance(self):
        import psutil

        core = self.core([[{"action": "escalate"}]], lease=1.5)
        first = core.execute(BrowserTask.from_dict(self.task(without_order=True)))
        chain = core.chains[first["chain_id"]]
        pid = chain.session.session["pid"]
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm"})
        self.assertEqual(progress["phase"], "awaiting_proposal")

        def alive():
            try:
                return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
            except psutil.Error:
                return False

        # Closing Chrome takes longer on Windows than on Linux; the chain is marked closed before the attempt's
        # result.json is written.
        result_file = self.attempt_dir(first, 2) / "result.json"
        deadline = time.monotonic() + 20
        while (not chain.closed or alive() or not result_file.exists()) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertTrue(chain.closed)
        self.assertFalse(alive())
        stored = json.loads(result_file.read_text())
        self.assertEqual((stored["attempt_status"], stored["stop_reason"], stored["handoff"]["reason"]),
                         ("incomplete", "needs_handoff", "lease_expired"))
        self.assertFalse(stored["handoff"]["continuation"]["allowed"])
        late = core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"],
                            "operation": "step", "proposal": {}})
        self.assertIn("error", late)

    def test_a_revision_may_not_widen_authority(self):
        core = self.core([[{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(self.task(without_order=True)))
        wider = self.task(revision=2, max_steps=100)
        refused = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev", "task": wider})
        self.assertEqual(refused["error"], {"code": "revision_refused", "reason": "budget_widened"})
        same = self.task(revision=1)
        self.assertEqual(core.resume({**self.resume_token(first), "operation": "start", "mode": "jev",
                                      "task": same})["error"]["reason"], "revision_not_newer")

    def test_a_driver_level_unknown_effect_does_not_hand_the_session_over(self):
        task = self.task()
        core = self.core([TO_ORDER + SAVE])
        broken = {}

        def fail_save(calls, provider):
            run = core.chains[next(iter(core.chains))].runs[0]
            if calls == 4 and not broken:
                original = run.adapter.execute_prepared

                def boom(prepared, action_id):
                    raise ValueError("lost response")
                broken["x"] = original
                run.adapter.execute_prepared = boom

        from reflexmesh.adapters.system_one.decision_providers import LabelScriptProvider

        class Hooked(LabelScriptProvider):
            def decide(self, state, questions):
                fail_save(self.calls, self)
                return super().decide(state, questions)

        core.provider_factory = lambda: (Hooked(TO_ORDER + SAVE), "script:label", None)
        result = core.execute(BrowserTask.from_dict(task))
        self.assertIn("error", [a["driver_outcome"] for a in result["actions"]])   # the Save really failed
        self.assertNotEqual(result["cleanup"]["status"], "retained")
        handoff = result["handoff"] or {}
        self.assertFalse((handoff.get("continuation") or {"allowed": False})["allowed"])


if __name__ == "__main__":
    unittest.main()
