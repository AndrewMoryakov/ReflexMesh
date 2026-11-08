"""M3: handoff and continuation in the same browser session (V0.6 sections 10 and 14), on a real Chromium.

The deterministic label script stands in for Jev and the test itself for the calling LLM; what is checked is the
runtime: the same browser session across attempts, a new attempt and owner epoch, one shared budget, a one-time
token with a lease, and an old owner that can no longer act.
"""

import contextlib
import copy
import importlib.util
import io
import asyncio
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
        core = self.core([[{"action": "escalate"}], TO_ORDER + SAVE])
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

    def test_restart_does_not_make_the_same_typing_a_new_operation(self):
        """Section 9: typing may have a server effect (autosave) and nothing settles it mid-chain, so the same slot
        in the same field on the same document stays a repeat after a restart. Until the review of PR #13 a restart
        forgot it and this chain completed."""
        core = self.core([TO_ORDER[:1] + [{"action": "escalate"}], TO_ORDER[:1] * 3])
        first = core.execute(BrowserTask.from_dict(self.task()))
        self.assertEqual(first["handoff"]["reason"], "escalation_requested")
        second = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev", "restart": True})
        self.assertEqual((second["attempt_status"], second["handoff"]["reason"]), ("incomplete", "no_progress"))
        trace = [json.loads(line) for line in (self.attempt_dir(first, 2) / "trace.jsonl").read_text("utf-8").splitlines()]
        self.assertIn("repeated_mutation", [r.get("reason") for r in trace if r["kind"] == "admission_rejected"])
        restarted = next(r for r in trace if r["kind"] == "session_restarted")
        self.assertEqual(restarted["kept_typed"], 1)
        self.assertEqual(SITE.NOTES, {})

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

    def test_a_slot_is_matched_against_the_whole_field_name_not_the_cut_one(self):
        """Review of #14: names are cut to 80 characters for display, `fields` take up to 200. Matching the cut name
        hid a field with a longer name from its own slot, and two fields sharing the first 80 looked the same."""
        label = "Instructions for the courier who brings this order to the back entrance of the building"
        data = self.task()
        data["start_url"] = self.origin + "/long-fields"
        for slot in data["text_slots"]:
            if slot["id"] == "note":
                slot["fields"] = [label + " (morning)"]
        core = self.core([[{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(data))
        self.assertEqual(first["handoff"]["fields_without_slot"], [(label + " (evening)")[:80]])
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm"})
        typing = [o for o in progress["action_options"] if o["operation"] == "type_text"]
        self.assertEqual([o["slot_refs"] for o in typing], [["note@1"]])
        core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"], "operation": "stop"})

    def test_a_field_renamed_after_the_observation_is_stale_even_past_eighty_characters(self):
        """Review of #14: the check before typing compared the signature, whose name is cut to 80 characters, so a
        label changed after them kept the slot allowed for the old name. Renamed between observing and typing."""
        label = "Instructions for the courier who brings this order to the back entrance of the building"
        data = self.task()
        data["start_url"] = self.origin + "/long-fields"
        for slot in data["text_slots"]:
            if slot["id"] == "note":
                slot["fields"] = [label + " (morning)"]
        core = self.core([[{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(data))
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm"})
        morning = next(o for o in progress["action_options"] if o["operation"] == "type_text")
        adapter = core.chains[first["chain_id"]].assist.adapter

        async def rename():
            cdp = await adapter.backend._session.get_or_create_cdp_session()
            await cdp.cdp_client.send.Runtime.evaluate(
                params={"expression": "document.querySelector('label[for=morning]').textContent += ' and later (evening)'"},
                session_id=cdp.session_id)

        adapter.backend._run(rename())
        step = core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"], "operation": "step",
                            "proposal": {"observation_id": progress["observation"]["observation_id"], "operation": "type_text",
                                         "option_ref": morning["ref"], "slot_ref": "note@1"}})
        self.assertEqual(step["last_proposal"], {"accepted": True, "dispatched": False, "rejected": "stale_target"})
        core.resume({"token": step["continuation_token"], "parent_attempt_id": step["attempt_id"], "operation": "stop"})

    def test_a_frame_reloaded_after_the_observation_makes_its_target_stale(self):
        """F11 and F02: a field inside a same-origin frame, observed; the frame reloads; the typing is refused, not sent
        into the new document."""
        data = self.task()
        data["start_url"] = self.origin + "/f11/same"
        data["text_slots"] = [{"id": "word", "version": 1, "value": "kiwi", "sensitivity": "normal"}]
        data["criteria"] = [{"id": "seen", "predicate": "page_match",
                             "args": {"anchor_slot_ref": "word@1", "field_name": "Word", "expected_slot_ref": "word@1"}}]
        SITE.F11_HITS.clear()
        core = self.core([[{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(data))
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm"})
        word = next(o for o in progress["action_options"] if o["operation"] == "type_text")
        adapter = core.chains[first["chain_id"]].assist.adapter

        async def reload_frame():
            cdp = await adapter.backend._session.get_or_create_cdp_session()
            await cdp.cdp_client.send.Runtime.evaluate(
                params={"expression": "new Promise(r => { const f = document.querySelector('iframe'); f.onload = () => r(1); f.contentWindow.location.reload(); })",
                        "awaitPromise": True}, session_id=cdp.session_id)

        adapter.backend._run(reload_frame())
        step = core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"], "operation": "step",
                            "proposal": {"observation_id": progress["observation"]["observation_id"], "operation": "type_text",
                                         "option_ref": word["ref"], "slot_ref": "word@1"}})
        self.assertEqual(step["last_proposal"], {"accepted": True, "dispatched": False, "rejected": "stale_target"})
        core.resume({"token": step["continuation_token"], "parent_attempt_id": step["attempt_id"], "operation": "stop"})
        self.assertEqual(SITE.F11_HITS, [])

    def test_a_document_navigated_after_the_observation_makes_its_target_stale(self):
        """F02: the order page is observed with its Save button; then the whole document is replaced, first by a
        reload of the same order (the very same button again), then by another order's page (a button of the same
        name). Each time, the click proposed on the observation before the navigation is refused, and nothing is
        pressed in the new document."""
        SITE.NOTES.clear()
        core = self.core([[{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(self.f06_task("125")))
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm"})
        adapter = core.chains[first["chain_id"]].assist.adapter

        async def navigate(expression):
            cdp = await adapter.backend._session.get_or_create_cdp_session()

            async def evaluate(code):
                reply = await cdp.cdp_client.send.Runtime.evaluate(
                    params={"expression": code, "returnByValue": True}, session_id=cdp.session_id)
                return reply.get("result", {}).get("value")

            await evaluate("window.__observed = 1; " + expression)
            for _ in range(100):          # the new document: the marker is gone and it has loaded
                await asyncio.sleep(0.1)
                try:
                    if await evaluate("window.__observed === undefined && document.readyState === 'complete'"):
                        return
                except Exception:
                    pass
            raise AssertionError("the document did not navigate")

        for expression, landing in (("location.reload()", "/orders/125"), ("location.href = '/orders/126'", "/orders/126")):
            with self.subTest(expression):
                save = next(o for o in progress["action_options"] if o["operation"] == "click" and "Save note" in o["name"])
                adapter.backend._run(navigate(expression))
                self.assertEqual([r["path"] for r in SITE.REQUESTS if r["method"] == "GET"][-1], landing)
                progress = core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"],
                                        "operation": "step",
                                        "proposal": {"observation_id": progress["observation"]["observation_id"],
                                                     "operation": "click", "option_ref": save["ref"]}})
                self.assertEqual(progress["last_proposal"], {"accepted": True, "dispatched": False, "rejected": "stale_target"})
        core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"], "operation": "stop"})
        self.assertEqual([r["path"] for r in SITE.REQUESTS if r["method"] == "POST" and not r["path"].startswith("/__")], [])
        self.assertEqual(SITE.NOTES, {})

    F06_NOTE = [{"action": "type_text", "field": "textbox 'Note'", "value": "^note@1"}, {"action": "click", "element": "Save note"}]

    def f06_task(self, order, **changes):
        """The verified note task on one order: 301 loses the answer to its note, 302 autosaves its note field."""
        data = self.task(order=order, **changes)
        data["start_url"] = self.origin + f"/orders/{order}"
        for slot in data["text_slots"]:
            if slot["id"] == "record":
                slot["value"] = f"Заказ №{order}"
        data["criteria"][1]["args"]["subject"] = order
        return data

    def f06_saves(self, core):
        """Save note clicks the runtime dispatched in the whole chain, and POSTs the site received for the note."""
        dispatched = sum(1 for t in Path(self.out.name).rglob("trace.jsonl") for line in t.read_text("utf-8").splitlines()
                         if (e := json.loads(line))["kind"] == "dispatch_committed" and e.get("operation") == "click")
        return dispatched, sum(1 for r in self.posts() if r["path"] == "/orders/301/note")

    def rejections(self, first, n):
        trace = (self.attempt_dir(first, n) / "trace.jsonl").read_text("utf-8").splitlines()
        return [json.loads(line).get("reason") for line in trace if json.loads(line)["kind"] == "admission_rejected"]

    def assert_lost_response_held(self, core, last):
        """One Save sent by the runtime, and the form sent once: every POST carries the same submission id. The same
        request may still reach the site more than once (2 or 3 POSTs for one click on Windows with Chrome 142); once
        the browser has stopped, the registered check reports that as a failed repeat. While the chain holds the
        session it settles nothing, so the repeats stay unknown."""
        clicks, posts = self.f06_saves(core)
        self.assertEqual(clicks, 1)
        self.assertGreaterEqual(posts, 1)
        submissions = SITE.SUBMISSIONS.get("301", [])
        self.assertEqual(len(submissions), posts)
        self.assertEqual(len(set(submissions)), 1, submissions)
        self.assertNotIn("", submissions)   # the page's script ran
        if posts > 1:                        # a resent request is never settled as completed
            self.assertNotEqual(last.get("attempt_status"), "completed")
        rows = {r["id"]: r["status"] for r in last.get("verification") or []}
        if rows:
            stopped = (last.get("cleanup") or {}).get("status") != "retained"
            expected = "fail" if posts > 1 and stopped else ("unknown" if posts > 1 else rows["runtime.no_uncertain_repeats"])
            self.assertEqual(rows["runtime.no_uncertain_repeats"], expected)

    def settled_on_the_saved_page(self, result):
        """The other lawful path: the page reached after going back or restarting already shows the saved note, so
        the loop stops for verification before any second Save is proposed, and the one POST settles it. Seen with
        chrome-headless-shell, whose back navigation reloads the page and which does not resend the lost request;
        Chrome and Edge restore the page without the note, and the second Save is proposed and refused."""
        return (result.get("attempt_status"), result.get("stop_reason")) == ("completed", "verified") and \
            self.f06_saves(None)[1] == 1

    def test_f06_a_lost_response_is_not_resent_by_going_back_and_saving_again(self):
        core = self.core([self.F06_NOTE + [{"action": "go_back"}, {"action": "click", "element": "Save note"}, {"action": "finish"}]])
        first = core.execute(BrowserTask.from_dict(self.f06_task("301")))
        if not self.settled_on_the_saved_page(first):
            self.assertIn("repeated_mutation", self.rejections(first, 1))
        self.assert_lost_response_held(core, first)

    def test_f06_a_lost_response_is_not_resent_after_a_handoff_with_restart(self):
        core = self.core([self.F06_NOTE + [{"action": "escalate"}], self.F06_NOTE + [{"action": "finish"}]])
        first = core.execute(BrowserTask.from_dict(self.f06_task("301")))
        second = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev", "restart": True})
        if not self.settled_on_the_saved_page(second):
            self.assertEqual(self.rejections(first, 2), ["repeated_mutation", "repeated_mutation"])   # the typing and the Save
        self.assert_lost_response_held(core, second)

    def test_f06_a_lost_response_is_not_resent_by_llm_assistance(self):
        core = self.core([self.F06_NOTE + [{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(self.f06_task("301")))
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm"})
        back = next(o for o in progress["action_options"] if o["operation"] == "go_back")
        progress = core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"], "operation": "step",
                                "proposal": {"observation_id": progress["observation"]["observation_id"], "operation": "go_back", "option_ref": back["ref"]}})
        if self.settled_on_the_saved_page(progress):
            self.assert_lost_response_held(core, progress)
            return
        save = next(o for o in progress["action_options"] if o.get("name") == "Save note")
        step = core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"], "operation": "step",
                            "proposal": {"observation_id": progress["observation"]["observation_id"], "operation": "click", "option_ref": save["ref"]}})
        self.assertEqual(step["last_proposal"], {"accepted": True, "dispatched": False, "rejected": "repeated_mutation"})
        done = core.resume({"token": step["continuation_token"], "parent_attempt_id": step["attempt_id"], "operation": "stop"})
        self.assert_lost_response_held(core, done)

    def test_f06_a_key_press_proposed_in_assistance_is_refused(self):
        """F06, Enter: even with the permission, press_key is not a capability and cannot be proposed."""
        core = self.core([TO_ORDER[:1] + [{"action": "escalate"}]])
        data = self.task()
        data["permissions"] = data["permissions"] + ["press_key"]
        first = core.execute(BrowserTask.from_dict(data))
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm"})
        self.assertNotIn("press_key", {o["operation"] for o in progress["action_options"]})
        field = next(o for o in progress["action_options"] if o["operation"] == "type_text")
        step = core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"], "operation": "step",
                            "proposal": {"observation_id": progress["observation"]["observation_id"], "operation": "press_key",
                                         "option_ref": field["ref"]}})
        self.assertFalse(step["last_proposal"].get("dispatched", False))
        core.resume({"token": step["continuation_token"], "parent_attempt_id": step["attempt_id"], "operation": "stop"})
        self.assertEqual([r["path"] for r in SITE.REQUESTS if r["path"] == "/orders"], ["/orders"])   # nothing submitted

    def test_f06_an_autosaving_field_is_not_typed_again_and_nothing_claims_completion(self):
        """F06, autosave: a script sends the field to the server; no form says so. The same typing is not dispatched
        again, and although every criterion passes, the registered check sees a POST no submit accounts for and
        leaves the repeats unknown: no completed."""
        typing, save = self.F06_NOTE
        core = self.core([[typing, typing, save, {"action": "finish"}]])
        done = core.execute(BrowserTask.from_dict(self.f06_task("302")))
        self.assertIn("repeated_mutation", self.rejections(done, 1))
        self.assertEqual(len([a for a in done["actions"] if a["operation"] == "type_text"]), 1)
        # How many autosaves one typing makes is the page's and the driver's business (Browser Use clears the field
        # first; the page saw '' and the note twice for one dispatch); the runtime's part is one dispatch.
        self.assertIn("Перезвонить завтра", SITE.AUTOSAVED.get("302", []))
        self.assertEqual((done["attempt_status"], done["stop_reason"]), ("incomplete", "verification_unknown"))
        rows = {r["id"]: r["status"] for r in done["verification"]}
        self.assertEqual((rows["note_saved"], rows["note_saved_once"]), ("pass", "pass"))
        self.assertEqual(rows["runtime.no_uncertain_repeats"], "unknown")

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

    def test_a_new_slot_version_is_not_applied_to_an_observation_made_before_the_revision(self):
        """F03: the note is observed under revision 1 (note@1); the operator stops, revises the note to note@2 and
        resumes in the same session. Typing note@2 on the observation made before the revision is refused, and so is
        the old note@1 on the new observation; note@2 on the new observation goes through."""
        SITE.NOTES.clear()
        core = self.core([[{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(self.f06_task("125")))
        before = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm"})
        stopped = core.resume({"token": before["continuation_token"], "parent_attempt_id": before["attempt_id"],
                               "operation": "stop"})
        revised = self.f06_task("125", revision=2)
        note = next(s for s in revised["text_slots"] if s["id"] == "note")
        note.update(version=2, value="Перезвонить в пятницу")
        for criterion in revised["criteria"]:
            for key, value in criterion["args"].items():
                if value == "note@1":
                    criterion["args"][key] = "note@2"
                elif type(value) is list:
                    criterion["args"][key] = ["note@2" if v == "note@1" else v for v in value]
        after = core.resume({**self.resume_token(stopped), "operation": "start", "mode": "llm", "task": revised})
        self.assertNotIn("error", after)
        old_id, new_id = before["observation"]["observation_id"], after["observation"]["observation_id"]
        self.assertNotEqual(old_id, new_id)
        field = next(o for o in after["action_options"] if o["operation"] == "type_text" and "Note" in o["name"])

        def propose(progress, observation_id, slot_ref):
            return core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"],
                                "operation": "step", "proposal": {"observation_id": observation_id, "operation": "type_text",
                                                                  "option_ref": field["ref"], "slot_ref": slot_ref}})

        stale = propose(after, old_id, "note@2")
        self.assertEqual(stale["last_proposal"], {"rejected": "stale_observation"})
        old = propose(stale, new_id, "note@1")
        self.assertEqual(old["last_proposal"], {"rejected": "unknown_slot"})
        typed = propose(old, new_id, "note@2")
        self.assertEqual(typed["last_proposal"]["dispatched"], True)
        core.resume({"token": typed["continuation_token"], "parent_attempt_id": typed["attempt_id"], "operation": "stop"})
        self.assertEqual(SITE.NOTES, {})            # typed, never saved: no Save was proposed

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
        # Section 12: the assistant's requests are on the chain's model-call counter, reserved before each request.
        self.assertEqual(done["chain_budget"]["used"]["model_calls"], first["budget"]["model_calls"] + 4)

    def test_the_m5_assistant_stops_when_the_shared_model_call_budget_is_spent(self):
        """Review of #15: the assistant's calls bypassed max_model_calls; a reservation now comes first."""
        spec = importlib.util.spec_from_file_location("m5_runner", ROOT / "experiments/v06/m5/runner.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)

        class Client:
            asked = 0

            def ask(self, system, content):
                Client.asked += 1
                ref = next(x["ref"] for x in content["action_options"] if x.get("ref") and x["operation"] == "click")
                return {"option_ref": ref}, {"input_tokens": 1, "output_tokens": 1, "cost": 0.0}, "m", "r", 1

        core = self.core([[{"action": "escalate"}], [{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(self.task(max_model_calls=2)))
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm"})
        runner.Assistant(Client(), "goal").help(core, progress)
        self.assertEqual(Client.asked, 2 - first["budget"]["model_calls"])

    def test_going_back_in_assistance_is_a_step_and_does_not_end_the_attempt(self):
        core = self.core([TO_ORDER + [{"action": "escalate"}]])
        first = core.execute(BrowserTask.from_dict(self.task()))
        self.assertEqual(first["handoff"]["page"], self.origin + "/orders/125")
        progress = core.resume({**self.resume_token(first), "operation": "start", "mode": "llm"})
        back = next(o for o in progress["action_options"] if o["operation"] == "go_back")
        moved = core.resume({"token": progress["continuation_token"], "parent_attempt_id": progress["attempt_id"], "operation": "step",
                             "proposal": {"observation_id": progress["observation"]["observation_id"], "operation": "go_back", "option_ref": back["ref"]}})
        self.assertEqual(moved["schema_version"], "browser-progress/0.1")
        self.assertEqual(moved["last_proposal"], {"accepted": True, "dispatched": True})
        self.assertRegex(moved["observation"]["page"], r"/orders\b")
        self.assertEqual(moved["assistance_steps_remaining"], 4)
        core.resume({"token": moved["continuation_token"], "parent_attempt_id": moved["attempt_id"], "operation": "stop"})

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
        # The lease is 1 s. Closing stops the browser first and releases the site's claim after it, so both are
        # awaited with a bound rather than read after a fixed pause (a slow browser stop once outlasted 2.5 s).
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (not chain.closed or SITE.CLAIM):
            time.sleep(0.1)
        self.assertTrue(chain.closed)
        self.assertEqual(SITE.CLAIM, {})
        self.assertFalse(psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE)
        refused = core.resume({**self.resume_token(first), "operation": "start", "mode": "jev"})
        self.assertIn(refused["error"]["code"], ("chain_closed", "session_closed", "token_expired"))

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


def tearDownModule():
    from v06_samples import COMPLETED_CHECKED
    print(f"F12: {len(COMPLETED_CHECKED)} completed results checked for evidence so far", file=sys.stderr)
