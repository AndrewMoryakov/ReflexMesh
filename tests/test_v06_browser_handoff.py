"""M3: handoff and continuation in the same browser session (V0.6 sections 10 and 14), on a real Chromium.

The deterministic label script stands in for Jev and the test itself for the calling LLM; what is checked is the
runtime: the same browser session across attempts, a new attempt and owner epoch, one shared budget, a one-time
token with a lease, and an old owner that can no longer act.
"""

import copy
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
        stored = json.loads((Path(self.out.name) / "chain/attempt-1/result.json").read_text())
        self.assertEqual(stored["attempt_status"], "incomplete")
        self.assertEqual(stored["handoff"]["continuation"]["token"], "[withheld]")   # never on disk
        self.assertEqual(core.resume({**self.resume_token(first), "operation": "start", "mode": "jev"})["error"]["code"],
                         "token_invalid")
        self.assertEqual(SITE.NOTES, {"125": ["Перезвонить завтра"]})
        self.assertEqual(SITE.CLAIM, {})

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
        assisted = json.loads((Path(self.out.name) / "chain/attempt-2/result.json").read_text())
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
        time.sleep(3.0)
        self.assertTrue(chain.closed)
        self.assertFalse(psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE)
        stored = json.loads((Path(self.out.name) / "chain/attempt-2/result.json").read_text())
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
