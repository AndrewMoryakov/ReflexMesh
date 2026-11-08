"""The generic browser path against a real Chromium, Browser Use and SystemOneHarness.

Runs only when REFLEXMESH_CHROME names a Chromium and the pinned dependencies are installed;
otherwise these checks are pending for that environment, not passed. The provider here is the
deterministic label script: it proves the mechanics (observation, admission, dispatch, effects,
cleanup) on a page with no ReflexMesh markup. It says nothing about Jev.

REFLEXMESH_CHROME_NO_SANDBOX=1 starts Chromium without its sandbox, for hosts that forbid it.
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reflexmesh.contracts.browser import BrowserTask
from reflexmesh.runtime.browser_run import BrowserRun
from v06_samples import task_dict

CHROME = os.environ.get("REFLEXMESH_CHROME")
SANDBOX = os.environ.get("REFLEXMESH_CHROME_NO_SANDBOX") != "1"
AVAILABLE = bool(CHROME and Path(CHROME).exists() and importlib.util.find_spec("browser_use")
                 and importlib.util.find_spec("systemone_harness"))

SITE_SPEC = importlib.util.spec_from_file_location("order_site", ROOT / "experiments/v06/site/server.py")
SITE = importlib.util.module_from_spec(SITE_SPEC)
SITE_SPEC.loader.exec_module(SITE)

HAPPY_PATH = [
    {"action": "type_text", "field": "Order number", "value": "^order@1"},
    {"action": "click", "element": "button 'Search'"},
    {"action": "click", "element": "Заказ №125"},
    {"action": "type_text", "field": "textbox 'Note'", "value": "^note@1"},
    {"action": "click", "element": "Save note"},
    {"action": "finish"},
]


def label_script(steps, before=None, record=None):
    from reflexmesh.adapters.system_one.decision_providers import LabelScriptProvider

    class Scripted(LabelScriptProvider):
        def decide(self, state, questions):
            if record is not None:
                record.append(json.dumps([state, questions], ensure_ascii=False))
            if before is not None:
                before(self.calls, self)
            return super().decide(state, questions)

    return Scripted(steps)


class BrowserCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SITE.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        SITE.NOTES.clear()
        SITE.AUTOSAVED.clear()
        SITE.SUBMISSIONS.clear()
        SITE.FX_HITS.clear()
        SITE.F11_HITS.clear()
        SITE.DRAFTS.clear()
        SITE.META.clear()
        SITE.REQUESTS.clear()
        SITE.CLAIM.clear()
        SITE.IN_FLIGHT[0] = 0
        self.out = tempfile.TemporaryDirectory()
        self.addCleanup(self.out.cleanup)

    def task(self, start="/orders", **changes):
        return BrowserTask.from_dict(task_dict(start_url=self.origin + start, allowed_origins=[self.origin], **changes))

    def start(self, task, provider, **options):
        self.provider = provider
        self.run_ = BrowserRun(task, chrome=CHROME, provider_factory=lambda: (provider, "script:label", None),
                               action_provider="script", out_dir=Path(self.out.name) / "run", sandbox=SANDBOX,
                               **options)
        return self.run_

    def trace(self):
        return [json.loads(line) for line in (Path(self.out.name) / "run" / "trace.jsonl").read_text("utf-8").splitlines()]

    def posts(self):
        return [r for r in SITE.REQUESTS if r["method"] == "POST"]


@unittest.skipUnless(AVAILABLE, "set REFLEXMESH_CHROME and install the pinned Browser Use + SystemOneHarness")
class GenericPath(BrowserCase):
    def test_unknown_site_scenario_runs_through_gate_and_leaves_the_right_evidence(self):
        result = self.start(self.task(), label_script(HAPPY_PATH)).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "verification_unknown"))
        self.assertEqual(result["task_outcome"], "unknown")  # no verifier yet: never `completed`
        self.assertEqual(SITE.NOTES, {"125": ["Перезвонить завтра"]})
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(result["budget"]["dispatches"], 6)
        self.assertEqual(result["budget"]["model_calls"], 0)
        rows = {r["id"]: r["status"] for r in result["verification"]}
        for ok in ("runtime.permissions", "runtime.immutable_slots", "runtime.dispatch_ordering", "runtime.budget",
                   "runtime.ownership"):
            self.assertEqual(rows[ok], "pass", ok)
        # No registered source saw the server side, and this task's anchor ("125") names no element whole.
        for unproven in ("runtime.no_uncertain_repeats", "note_saved"):
            self.assertEqual(rows[unproven], "unknown", unproven)
        self.assertEqual(result["cleanup"]["status"], "closed")
        self.assertTrue(result["cleanup"]["profile_removed"])
        kinds = [r["kind"] for r in self.trace()]
        for expected in ("task_accepted", "attempt_started", "observed", "options_built", "decision_requested",
                         "decision_received", "threshold_judged", "dispatch_committed", "driver_returned",
                         "effect_assessed", "criterion_assessed", "terminal"):
            self.assertIn(expected, kinds)
        typed = [a for a in result["actions"] if a["operation"] == "type_text"]
        self.assertEqual([a["slot_ref"] for a in typed], ["order@1", "note@1"])
        self.assertTrue(all(a["ui_effect"]["field_shows_slot_value"] for a in typed))
        self.assertTrue(all(a["effect"] == "unknown" for a in result["actions"] if a["operation"] != "navigate"))

    def anchored(self, **changes):
        """The demo task with the anchor V0.6 section 11 describes: a slot holding the order's own title."""
        data = task_dict(start_url=self.origin + "/orders", allowed_origins=[self.origin], **changes)
        data["text_slots"].append({"id": "record", "version": 1, "value": "Заказ №125", "sensitivity": "normal"})
        data["criteria"][0]["args"]["anchor_slot_ref"] = "record@1"
        return BrowserTask.from_dict(data)

    def test_the_runtime_verifies_the_saved_note_by_reopening_the_order(self):
        """M4: the criterion is judged on the page after every step, and the verifier itself reopens the
        order. The script never gets to its `finish`: the runtime stopped as soon as the note was there."""
        result = self.start(self.anchored(), label_script(HAPPY_PATH)).run()
        rows = {r["id"]: r for r in result["verification"]}
        self.assertEqual((rows["note_saved"]["status"], rows["note_saved"]["scope"]), ("pass", "ui_after_reopen"))
        self.assertEqual(len(self.posts()), 1)
        reopen = result["actions"][-1]
        self.assertEqual((reopen["operation"], reopen["producer"], reopen["effect"]), ("navigate", "verifier", "applied"))
        self.assertEqual([r["path"] for r in SITE.REQUESTS if r["method"] == "GET"].count("/orders/125"), 3)
        stages = [(r["status"], r["reason"]) for r in self.trace() if r["kind"] == "criterion_assessed"]
        self.assertEqual(stages, [("unknown", "no_anchor"), ("unknown", "no_field"), ("fail", "value_differs"),
                                  ("unknown", "not_reopened"), ("unknown", "not_reopened"), ("pass", "value_matches")])
        # Verified on the page, still not `completed`: ownership, repeats and click effects are unproven here.
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("incomplete", "verification_unknown", "unknown"))
        self.assertNotIn("Customer: Petrov", (Path(self.out.name) / "run" / "trace.jsonl").read_text("utf-8"))

    def test_a_finish_before_saving_is_a_confirmed_failure(self):
        early = HAPPY_PATH[:3] + [{"action": "finish"}]
        result = self.start(self.anchored(), label_script(early)).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("failed", "postcondition_failed", "fail"))
        self.assertEqual(self.posts(), [])

    def test_a_finish_on_the_results_page_cannot_be_judged(self):
        """The early DONE of laya-browser: the order is listed, its notes are not on this page."""
        early = HAPPY_PATH[:2] + [{"action": "finish"}]
        result = self.start(self.anchored(), label_script(early)).run()
        rows = {r["id"]: r for r in result["verification"]}
        self.assertEqual((rows["note_saved"]["status"], rows["note_saved"]["reason"]), ("unknown", "no_field"))
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "verification_unknown"))

    def verified(self):
        """The anchored task plus the registered order-desk check, as a service would configure it."""
        from reflexmesh.verification.checks import OrderDeskCheck

        data = task_dict(start_url=self.origin + "/orders", allowed_origins=[self.origin])
        data["text_slots"].append({"id": "record", "version": 1, "value": "Заказ №125", "sensitivity": "normal"})
        data["criteria"][0]["args"]["anchor_slot_ref"] = "record@1"
        data["criteria"].append({"id": "note_saved_once", "predicate": "registered_check",
                                 "args": {"check_id": "desk", "subject": "125", "expected_slot_refs": ["note@1"]}})
        return BrowserTask.from_dict(data), {"desk": OrderDeskCheck("desk", self.origin)}

    def test_completed_needs_every_criterion_constraint_and_effect(self):
        """M4: the first path to `completed`. The registered source settles every effect and the repeats; the
        browser session's ownership is evidenced; both criteria pass."""
        task, checks = self.verified()
        result = self.start(task, label_script(HAPPY_PATH), checks=checks).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"], result["task_outcome"]),
                         ("completed", "verified", "pass"))
        rows = {r["id"]: r for r in result["verification"]}
        self.assertTrue(all(r["status"] == "pass" for r in rows.values()), rows)
        self.assertEqual(rows["runtime.ownership"]["scope"], "browser_session_launched_by_attempt")
        self.assertEqual(rows["runtime.no_uncertain_repeats"]["scope"], "registered_source")
        effects = [(a["operation"], (a.get("request") or {}).get("method"), a["effect"]) for a in result["actions"]]
        self.assertIn(("click", "POST", "applied"), effects)
        self.assertNotIn("unknown", [e for _, _, e in effects])
        self.assertEqual(SITE.CLAIM, {})                      # released
        self.assertNotIn(checks["desk"]._token or "no-token", json.dumps(result))

    def test_a_browser_process_still_exiting_at_close_does_not_decide_the_verdict(self):
        """A child of the closed browser may outlive the close by moments. Judged in that moment, ownership was
        unknown and the attempt incomplete, while the row assessed after cleanup said pass."""
        import reflexmesh.runtime.browser_run as module

        real, seen = module.processes_using, []

        def still_exiting_once(path):
            seen.append(path)
            return [os.getpid()] if len(seen) == 1 else real(path)

        task, checks = self.verified()
        module.processes_using = still_exiting_once
        try:
            result = self.start(task, label_script(HAPPY_PATH), checks=checks).run()
        finally:
            module.processes_using = real
        self.assertTrue(seen)
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("completed", "verified"))
        self.assertEqual(result["cleanup"]["status"], "closed")
        self.assertEqual([(r["processes"], r["left"]) for r in self.trace() if r["kind"] == "browser_exit_awaited"], [(1, 0)])

    def test_a_browser_process_that_stays_after_close_keeps_the_attempt_from_completed(self):
        """The wait is bounded and changes nothing for a process that does not go: no `completed`, and it is killed."""
        import reflexmesh.runtime.browser_run as module

        real = module.processes_using
        stuck = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(stuck.wait)
        self.addCleanup(stuck.kill)

        def never_exits(path):
            return [stuck.pid] if stuck.poll() is None else real(path)

        task, checks = self.verified()
        module.processes_using = never_exits
        try:
            result = self.start(task, label_script(HAPPY_PATH), checks=checks).run()
        finally:
            module.processes_using = real
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "verification_unknown"))
        self.assertEqual(result["cleanup"]["status"], "forced")
        waited = [r for r in self.trace() if r["kind"] == "browser_exit_awaited"]
        self.assertEqual([(r["processes"], r["left"]) for r in waited], [(1, 1)])

    def test_a_second_controller_on_the_debugging_port_is_a_violation(self):
        """M4c: another process holding a connection to the browser's CDP port is a second controller."""
        import subprocess

        task, checks = self.verified()
        holder = []

        def attach(calls, provider):
            if calls == 1 and not holder:
                port = self.run_.adapter.session["port"]
                holder.append(subprocess.Popen([sys.executable, "-c",
                    f"import socket,time; s=socket.create_connection(('127.0.0.1',{port})); time.sleep(60)"]))
                time.sleep(0.5)

        try:
            result = self.start(task, label_script(HAPPY_PATH, before=attach), checks=checks).run()
        finally:
            for process in holder:
                process.kill()
                process.wait()
        rows = {r["id"]: r for r in result["verification"]}
        self.assertEqual(rows["runtime.ownership"]["status"], "fail")
        self.assertIn("Another process", rows["runtime.ownership"]["reason"])
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("failed", "constraint_violated"))

    def test_a_busy_registered_source_blocks_before_the_browser_starts(self):
        from reflexmesh.verification.checks import OrderDeskCheck

        other = OrderDeskCheck("other", self.origin)
        other.claim("someone-else")
        try:
            task, checks = self.verified()
            result = self.start(task, label_script(HAPPY_PATH), checks=checks).run()
        finally:
            other.release()
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("blocked", "unsupported_criterion"))
        self.assertEqual(result["detail"]["detail"], "check_busy")
        self.assertEqual([r for r in SITE.REQUESTS if not r["path"].startswith("/__")], [])

    def test_an_unregistered_check_is_refused_whatever_the_task_says(self):
        task, _ = self.verified()
        result = self.start(task, label_script(HAPPY_PATH)).run()   # the service registered nothing
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("blocked", "unsupported_criterion"))

    def test_no_reflexmesh_markup_or_site_ids_are_used(self):
        import ast

        def literals(path):
            tree = ast.parse(path.read_text("utf-8"))
            docs = {id(node.body[0].value) for node in ast.walk(tree)
                    if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
                    and node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(getattr(node.body[0], "value", None), ast.Constant)}
            return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in docs]

        for name in ("adapters/system_one/browser_adapter.py", "runtime/browser_run.py",
                     "adapters/system_one/decision_providers.py"):
            used = [text for text in literals(ROOT / "src/reflexmesh" / name) if "reflex-id" in text.lower()]
            self.assertEqual(used, [], name)
        # and the demo site carries no ReflexMesh markup for the core to find
        self.assertNotIn("data-reflex", (ROOT / "experiments/v06/site/server.py").read_text("utf-8"))

    def test_a_node_replaced_after_observation_is_stale_and_not_dispatched(self):
        task = self.task()
        steps = [HAPPY_PATH[0], HAPPY_PATH[1]] + HAPPY_PATH[1:]  # Search is chosen twice: the first choice goes stale

        def replace_search(call, provider):
            if call == 1:  # the decision for the first Search click
                adapter = self.run_.adapter

                async def clone():
                    cdp = await adapter.backend._session.get_or_create_cdp_session()
                    await cdp.cdp_client.send.Runtime.evaluate(
                        params={"expression": "var b=document.querySelector('button'); b.replaceWith(b.cloneNode(true))"},
                        session_id=cdp.session_id)

                adapter.backend._run(clone())

        result = self.start(task, label_script(steps, before=replace_search)).run()
        trace = self.trace()
        kinds = [r["kind"] for r in trace]
        rejected = kinds.index("admission_rejected")
        self.assertEqual(trace[rejected]["reason"], "stale_target")
        self.assertEqual(kinds[rejected + 1], "reobserve")
        self.assertNotIn("dispatch_committed", kinds[rejected - 2:rejected])  # the proposal before it was never dispatched
        # the second choice is made on a fresh observation and goes through
        self.assertEqual(kinds[rejected + 2], "observed")
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(result["budget"]["dispatches"], 6)

    def test_text_slot_payload_is_exact_and_secret_values_stay_out_of_every_record(self):
        secret = "S3NTINEL-9f2a-do-not-log"
        slots = [{"id": "order", "version": 1, "value": "125", "sensitivity": "normal"},
                 {"id": "note", "version": 1, "value": secret, "sensitivity": "secret"}]
        seen: list[str] = []
        result = self.start(self.task(text_slots=slots), label_script(HAPPY_PATH, record=seen)).run()
        self.assertEqual(SITE.NOTES, {"125": [secret]})  # typed exactly
        self.assertEqual({r["id"]: r["status"] for r in result["verification"]}["runtime.immutable_slots"], "pass")
        self.assertTrue(seen)
        blob = "\n".join(seen) + json.dumps(result, ensure_ascii=False) + \
            (Path(self.out.name) / "run" / "trace.jsonl").read_text("utf-8")
        self.assertNotIn(secret, blob)
        self.assertIn("secret value, not shown", "\n".join(seen))

    def test_dropdown_and_checkbox_reach_the_server_with_the_note(self):
        steps = HAPPY_PATH[:3] + [
            {"action": "select_option", "option": "Priority.*High"},
            {"action": "click", "element": "Notify customer"},
            HAPPY_PATH[3], HAPPY_PATH[4], {"action": "finish"}]
        result = self.start(self.task(), label_script(steps)).run()
        why = (result["attempt_status"], result["stop_reason"], [a["operation"] for a in result["actions"]],
               [(r["kind"], r.get("reason"), r.get("error")) for r in self.trace() if r["kind"] in ("admission_rejected", "decision_failed")])
        self.assertEqual(SITE.NOTES, {"125": ["Перезвонить завтра"]}, why)
        self.assertEqual(SITE.META, {"125": {"priority": "High", "notify": True}})
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual([a["operation"] for a in result["actions"]],
                         ["navigate", "type_text", "click", "click", "select_option", "click", "type_text", "click"])
        self.assertTrue(all(a["driver_outcome"] == "returned" and a["driver_ok"] for a in result["actions"]))

    def test_scroll_wait_and_go_back_run_through_the_gate(self):
        steps = [{"action": "scroll", "direction": "down"}, {"action": "wait"},
                 {"action": "click", "element": "link 'Orders'"}, {"action": "go_back", "to": "/"},
                 {"action": "finish"}]
        result = self.start(self.task(start="/"), label_script(steps)).run()
        self.assertEqual([a["operation"] for a in result["actions"]], ["navigate", "scroll", "wait", "click", "go_back"])
        self.assertTrue(all(a["driver_ok"] for a in result["actions"]))
        self.assertEqual(result["budget"]["dispatches"], 5)
        # read-only operations leave the repeat and permission rows untouched
        rows = {r["id"]: r["status"] for r in result["verification"]}
        self.assertEqual(rows["runtime.permissions"], "pass")
        last_page = [r for r in self.trace() if r["kind"] == "observed"][-1]["page"]
        self.assertTrue(last_page.endswith("/"))  # back on the dashboard

    def test_the_same_action_on_an_unchanged_page_stops_as_no_progress(self):
        """SystemOneHarness hashes the observation to notice a repeat; a fresh observation id in the
        fields would make every state look new, so none is put there."""
        steps = [{"action": "wait"}, {"action": "wait"}, {"action": "finish"}]
        result = self.start(self.task(start="/"), label_script(steps)).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "needs_handoff"))
        self.assertEqual(result["handoff"]["reason"], "no_progress")
        self.assertEqual([a["operation"] for a in result["actions"]], ["navigate", "wait"])  # the repeat never ran
        self.assertNotEqual({r["id"]: r["status"] for r in result["verification"]}["runtime.no_uncertain_repeats"], "fail")

    def test_a_button_pressed_again_on_an_unchanged_page_is_refused_then_no_progress(self):
        """A laya run clicked Search 60 times until the deadline: Browser Use renumbers every control on
        every capture, so neither SOH's repeat check nor a raw-text page signature saw a repeat."""
        steps = HAPPY_PATH[:2] + [{"action": "click", "element": "button 'Search'"}] * 6 + [{"action": "finish"}]
        result = self.start(self.task(), label_script(steps)).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "needs_handoff"))
        self.assertEqual(result["handoff"]["reason"], "no_progress")
        searches = [a for a in result["actions"] if (a.get("target") or {}).get("name") == "Search"]
        self.assertEqual(len(searches), 2)        # once on the form, once on the results; then refused
        self.assertFalse(searches[1]["ui_effect"]["content_changed"])   # the reload showed the same page
        rejected = [r["reason"] for r in self.trace() if r["kind"] == "admission_rejected"]
        self.assertIn("repeated_action", rejected)

    def test_a_second_tab_is_offered_and_can_be_switched_to(self):
        steps = HAPPY_PATH[:3] + [{"action": "click", "element": "Open tracking"},
                                  {"action": "switch_tab", "tab": "orders/125$"}, {"action": "finish"}]
        result = self.start(self.task(), label_script(steps)).run()
        self.assertEqual([a["operation"] for a in result["actions"]][-2:], ["click", "switch_tab"])
        self.assertTrue(all(a["driver_ok"] for a in result["actions"]))
        observed = [r for r in self.trace() if r["kind"] == "observed"]
        self.assertTrue(observed[-1]["page"].endswith("/orders/125"))  # the original tab is in front again

    def test_a_dense_page_is_reported_partial_and_scrolling_reaches_more(self):
        """260 buttons: Browser Use lists only what is near the window. Saying `complete` would
        let a run conclude that nothing else exists (V0.6 section 6)."""
        steps = [{"action": "scroll", "direction": "down"}, {"action": "finish"}]
        result = self.start(self.task(start="/catalog"), label_script(steps)).run()
        trace = self.trace()
        observed = [r for r in trace if r["kind"] == "observed"]
        built = [r for r in trace if r["kind"] == "options_built"]
        coverage = observed[0]["coverage"]
        self.assertEqual(coverage["status"], "partial")
        self.assertIn("more_content_below", coverage["reasons"])
        self.assertEqual(coverage["next_window"], "scroll")
        self.assertLess(coverage["controls_offered"], 260)
        first = {o["name"] for o in built[0]["options"]}
        second = {o["name"] for o in built[1]["options"]}
        self.assertTrue(second - first, "scrolling one window should reveal controls that were not offered before")
        self.assertIn("more_content_above", observed[1]["coverage"]["reasons"])
        self.assertEqual(result["budget"]["dispatches"], 2)

    def test_scrolling_down_again_is_not_a_repeat_while_the_window_moves(self):
        """M5 `quotes-scroll`: the page signature has no scroll offset, and on a page whose text stays the same
        the second scroll down was refused as `repeated_action` in every run."""
        steps = [{"action": "scroll", "direction": "down"}] * 3 + [{"action": "finish"}]
        result = self.start(self.task(start="/tall"), label_script(steps)).run()
        trace = self.trace()
        self.assertEqual([r["reason"] for r in trace if r["kind"] == "admission_rejected"], [])
        self.assertEqual([a["operation"] for a in result["actions"]], ["navigate", "scroll", "scroll", "scroll"])

    F11_STEPS = [{"action": "type_text", "field": "Word", "value": "^word@1"}, {"action": "click", "element": "Send word"},
                 {"action": "finish"}]

    def f11_run(self, kind, query=""):
        """One attempt on an F11 probe page: type the word into the small form and send it."""
        SITE.F11_HITS.clear()
        SITE.REQUESTS.clear()
        self.out = tempfile.TemporaryDirectory()
        self.addCleanup(self.out.cleanup)
        task = self.task(start=f"/f11/{kind}{query}",
                         text_slots=[{"id": "word", "version": 1, "value": "kiwi", "sensitivity": "normal"}],
                         criteria=[{"id": "seen", "predicate": "page_match",
                                    "args": {"anchor_slot_ref": "word@1", "field_name": "Word", "expected_slot_ref": "word@1"}}])
        result = self.start(task, label_script(self.F11_STEPS)).run()
        trace = self.trace()
        observed = next(r for r in trace if r["kind"] == "observed")
        offered = {(o["operation"], o["name"]) for o in next(r for r in trace if r["kind"] == "options_built")["options"]}
        return result, observed["coverage"], offered

    def test_frame_content_of_another_origin_is_not_offered_and_nothing_reaches_it(self):
        """F11, negative. The page's origin is the only one allowed. A frame of another site (out of process) and a
        frame of another origin on the same site (in process) both received the typed value and sent it before."""
        other = ThreadingHTTPServer(("127.0.0.1", 0), SITE.Handler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        for kind, query in (("cross", ""), ("cross-port", f"?port={other.server_port}")):
            with self.subTest(kind):
                result, coverage, offered = self.f11_run(kind, query)
                self.assertNotIn(("click", "Send word"), offered)
                self.assertNotIn(("type_text", "Word"), offered)
                self.assertEqual(SITE.F11_HITS, [])
                self.assertEqual([r for r in SITE.REQUESTS if "/f11/hit" in r["path"]], [])
                self.assertIn("frame_content_not_offered", coverage["reasons"])      # said, not hidden
                self.assertEqual(coverage["frames_not_offered"], 1)
                # In process its field and button are read and dropped; out of process nothing of it is read. Another
                # port of the same site is always in process. Another site depends on the build: Chrome 142 and Edge 144
                # put it out of process, chrome-headless-shell 1243 keeps it in process; the checks above hold either way.
                if kind == "cross-port":
                    self.assertGreaterEqual(coverage["frame_nodes_omitted"], 2)
                self.assertEqual(result["budget"]["dispatches"], 1)                  # the first navigation only

    def test_same_origin_frame_content_is_offered_and_the_exact_value_lands_in_it(self):
        """F11, positive: a frame of the page's own origin. Its form resolves against the frame's document."""
        result, coverage, offered = self.f11_run("same")
        self.assertIn(("type_text", "Word"), offered)
        self.assertEqual(SITE.F11_HITS, [{"kind": "same", "word": "kiwi"}])
        self.assertEqual([a.get("request") for a in result["actions"] if a["operation"] == "click"],
                         [{"method": "POST", "path": "/f11/hit/same"}])
        self.assertNotIn("frame_content_not_offered", coverage["reasons"])         # nothing was left out
        self.assertEqual((coverage["frame_nodes_omitted"], coverage["frames_not_offered"]), (0, 0))

    def test_open_shadow_root_content_is_offered_and_the_exact_value_lands_in_it(self):
        """F11, positive: an open shadow root. Its form sits inside the root, so the submit is resolved there."""
        result, coverage, offered = self.f11_run("shadow-open")
        self.assertIn(("type_text", "Word"), offered)
        self.assertEqual(SITE.F11_HITS, [{"kind": "shadow-open", "word": "kiwi"}])
        self.assertEqual([a.get("request") for a in result["actions"] if a["operation"] == "click"],
                         [{"method": "POST", "path": "/f11/hit/shadow-open"}])
        self.assertEqual(coverage["status"], "complete")

    def test_declared_capabilities_are_the_probed_ones(self):
        """F11: the declaration matches the probes above, and every result carries it."""
        from reflexmesh.adapters.system_one.browser_adapter import CAPABILITIES

        self.assertEqual({k: CAPABILITIES[k] for k in ("frames", "cross_origin_frames", "open_shadow_dom")},
                         {"frames": True, "cross_origin_frames": False, "open_shadow_dom": True})
        result, _, _ = self.f11_run("same")
        self.assertEqual(result["components"]["capabilities"]["cross_origin_frames"], False)

    def test_typing_does_not_press_enter(self):
        """F06, Enter: press_key is not offered (declared false), and typing alone must not submit the form it is in."""
        steps = [{"action": "type_text", "field": "Order number", "value": "^order@1"}, {"action": "wait"}, {"action": "finish"}]
        self.start(self.task(), label_script(steps)).run()
        self.assertEqual([(r["method"], r["path"]) for r in SITE.REQUESTS if r["path"] == "/orders"], [("GET", "/orders")])
        offered = {o["operation"] for r in self.trace() if r["kind"] == "options_built" for o in r["options"]}
        self.assertNotIn("press_key", offered)

    def test_a_link_to_another_origin_is_refused_before_any_request(self):
        steps = [{"action": "click", "element": "External portal"}]
        result = self.start(self.task(start="/"), label_script(steps)).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("blocked", "policy_denied"))
        self.assertEqual(result["budget"]["dispatches"], 1)  # only the permitted first navigation
        self.assertFalse([r for r in SITE.REQUESTS if (r.get("host") or "").startswith("localhost")])

    def test_a_form_that_sends_to_another_origin_is_not_offered(self):
        """The check on a link's destination does not see a form's action, and the request would leave first."""
        result = self.start(self.task(start="/partner"), label_script([{"action": "click", "element": "Back to orders"}])).run()
        options = next(r["options"] for r in self.trace() if r["kind"] == "options_built")
        names = [o["name"] for o in options]
        self.assertIn("Back to orders", names)
        self.assertNotIn("Send to partner", names)
        self.assertEqual(result["actions"][-1]["request"], {"method": "GET", "path": "/orders"})
        self.assertFalse([r for r in SITE.REQUESTS if r["method"] == "POST" and not r["path"].startswith("/__")])

    def test_an_off_origin_redirect_is_detected_after_the_request_not_prevented(self):
        """Known limit, pinned so it cannot change unnoticed.

        Admission sees a link's href, not a server-side redirect. Browser Use's own domain guard
        also reacts after navigation completes. So the foreign origin receives one request, the run
        stops `blocked/policy_denied` on the next observation, and nothing further is dispatched.
        Preventing the request needs per-target Fetch interception, which this build does not have.
        """
        steps = [{"action": "click", "element": "Partner offer"}, {"action": "finish"}]
        result = self.start(self.task(start="/"), label_script(steps)).run()
        # Full Chrome (not the headless shell) also asks the page's origin for /favicon.ico on its own.
        reached = [r for r in SITE.REQUESTS if (r.get("host") or "").startswith("localhost") and
                   r.get("path") != "/favicon.ico"]
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("blocked", "policy_denied"))
        self.assertEqual(len(reached), 1, reached)  # detected, not prevented
        self.assertEqual(result["budget"]["dispatches"], 2)  # the first navigation and the click; nothing after

    def test_ungranted_operation_is_not_even_offered(self):
        permissions = ["navigate", "click", "scroll", "wait"]  # no type_text
        result = self.start(self.task(permissions=permissions), label_script(HAPPY_PATH)).run()
        self.assertEqual(result["attempt_status"], "failed")  # the script asks for an action that was not offered
        self.assertEqual(result["stop_reason"], "protocol_error")
        self.assertEqual(result["budget"]["dispatches"], 1)
        self.assertEqual(SITE.NOTES, {})

    def test_cancel_during_a_pending_decision_dispatches_nothing_new(self):
        release = threading.Event()

        def hang(call, provider):
            if call == 1:
                release.wait(30)

        self.addCleanup(release.set)
        run = self.start(self.task(), label_script(HAPPY_PATH, before=hang))
        box = {}
        worker = threading.Thread(target=lambda: box.setdefault("result", run.run()))
        worker.start()
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not any(r["kind"] == "decision_requested" for r in run.journal.records
                                                      if r.get("observation_id") == 2) if hasattr(run, "journal") else True:
            time.sleep(0.05)
        began = time.monotonic()
        run.cancel()
        worker.join(30)
        release.set()
        result = box["result"]
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("cancelled", "cancel_requested"))
        self.assertLess(time.monotonic() - began, 12)  # grace + kill bound, with the decision still hanging
        self.assertEqual(result["budget"]["dispatches"], 2)  # navigation and the first typed value only
        self.assertEqual(SITE.NOTES, {})

    def test_deadline_ends_the_attempt_even_if_the_decision_provider_hangs(self):
        release = threading.Event()

        def hang(call, provider):
            if call == 1:
                release.wait(30)

        self.addCleanup(release.set)
        limits = {**task_dict()["limits"], "wall_seconds": 3}
        began = time.monotonic()
        result = self.start(self.task(limits=limits), label_script(HAPPY_PATH, before=hang)).run()
        release.set()
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "deadline"))
        self.assertLess(time.monotonic() - began, 3 + 5 + 2 + 6)
        self.assertEqual(SITE.NOTES, {})

    def test_deadline_ends_the_attempt_even_if_the_first_navigation_never_loads(self):
        """M5: a start page that did not load held an attempt with a 240 s limit for 30 minutes. Browser Use's
        own stop waits behind the hung navigation, so the watchdog ends the browser's processes itself."""
        held = []
        hole = socket.socket()
        hole.bind(("127.0.0.1", 0))
        hole.listen(8)
        self.addCleanup(hole.close)
        self.addCleanup(lambda: [conn.close() for conn in held])

        def accept():                                     # takes the connection, never answers
            try:
                while True:
                    held.append(hole.accept()[0])
            except OSError:
                pass

        threading.Thread(target=accept, daemon=True).start()
        origin = f"http://127.0.0.1:{hole.getsockname()[1]}"
        task = BrowserTask.from_dict(task_dict(start_url=origin + "/", allowed_origins=[origin],
                                               limits={**task_dict()["limits"], "wall_seconds": 5}))
        began = time.monotonic()
        result = self.start(task, label_script(HAPPY_PATH)).run()
        took = time.monotonic() - began
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "deadline"))
        # Limit, grace, then the finish (about 2 s on Windows). Before the watchdog ended the processes this run
        # took 43 s there, and an M5 attempt 30 minutes.
        self.assertLess(took, 5 + 5 + 10, f"the attempt took {took:.1f} s")
        self.assertEqual(result["cleanup"]["browser_processes_left"], 0)


class FirstMatch:
    """A scripted provider for a page where the script's description matches more than one option: it takes the
    first in the observation's order. Only for proving what happens to such a choice, never as a way to label."""

    model = "script/first"

    def __init__(self, steps, before=None):
        self.steps, self.before, self.calls = [dict(s) for s in steps], before, 0

    def decide(self, state, questions):
        import re as _re
        from systemone_harness.provider import Decision
        from reflexmesh.adapters.system_one.decision_providers import _choice, answers_for_step

        if self.before is not None:
            self.before(self.calls, self)
        step = self.steps[self.calls] if self.calls < len(self.steps) else {"action": "finish"}
        self.calls += 1
        if not step.get("first"):
            answers = answers_for_step(step, questions)
        else:
            answers = answers_for_step({"action": step["action"]}, questions)
            key = f"{step['action']}__element"
            criteria = questions[key]["criteria"]
            answers[key] = _choice(criteria, next(k for k, d in criteria.items() if _re.search(step["element"], str(d))))
        return Decision(answers=answers, usage={"input_tokens": 0, "output_tokens": 0}, model=self.model,
                        request_id=f"first-{self.calls}", latency_ms=1, raw={})

    def close(self):
        pass


@unittest.skipUnless(AVAILABLE, "set REFLEXMESH_CHROME and install the pinned Browser Use + SystemOneHarness")
class FChecks(BrowserCase):
    """Section 16 items the order desk did not cover. Each names its gate and item."""

    WORD = [{"id": "word", "version": 1, "value": "kiwi", "sensitivity": "normal"}]

    def fx_task(self, kind, **changes):
        return self.task(start=f"/fx/{kind}", text_slots=self.WORD, criteria=[{"id": "seen", "predicate": "page_match",
                         "args": {"anchor_slot_ref": "word@1", "field_name": "Word", "expected_slot_ref": "word@1"}}], **changes)

    def offered(self):
        return [{(o["operation"], o["name"]) for o in r["options"]} for r in self.trace() if r["kind"] == "options_built"]

    def test_f01_a_field_a_script_adds_is_offered_once_it_exists_and_gets_the_exact_value(self):
        """F01, dynamic form: the comment field does not exist until the page's own script adds it."""
        steps = [{"action": "click", "element": "Add a comment"}, {"action": "type_text", "field": "Comment", "value": "^word@1"},
                 {"action": "click", "element": "Send comment"}, {"action": "finish"}]
        result = self.start(self.fx_task("dynamic"), label_script(steps)).run()
        first, *later = self.offered()
        self.assertNotIn(("type_text", "Comment"), first)                 # not there before the script ran
        self.assertTrue(any(("type_text", "Comment") in o for o in later))
        self.assertEqual([(h["kind"], h["word"]) for h in SITE.FX_HITS], [("dynamic", "kiwi")])
        self.assertEqual([a.get("request") for a in result["actions"] if a["operation"] == "click"][-1],
                         {"method": "POST", "path": "/fx/hit/dynamic"})

    def test_f02_a_replaced_twin_is_stale_and_its_identical_twin_is_not_pressed_instead(self):
        """F02, ambiguous target: two buttons with the same role and name. The chosen one is replaced after the
        observation; an identity by role and name alone would find the other one. Nothing may be pressed."""
        def replace_first(call, provider):
            if call == 0:
                adapter = self.run_.adapter

                async def clone():
                    cdp = await adapter.backend._session.get_or_create_cdp_session()
                    await cdp.cdp_client.send.Runtime.evaluate(
                        params={"expression": "var b=document.querySelector('button'); b.replaceWith(b.cloneNode(true))"},
                        session_id=cdp.session_id)

                adapter.backend._run(clone())

        steps = [{"action": "click", "element": r"^button 'Send'", "first": True}, {"action": "finish"}]
        self.start(self.fx_task("twins"), FirstMatch(steps, before=replace_first)).run()
        self.assertEqual(SITE.FX_HITS, [])
        self.assertIn("stale_target", [r.get("reason") for r in self.trace() if r["kind"] == "admission_rejected"])

    def test_f04_a_tab_of_another_origin_opened_by_the_page_is_never_acted_on(self):
        """F04, a foreign tab: the page itself opens a tab of another origin (localhost, not 127.0.0.1). It is not
        offered as a tab, and nothing typed or pressed reaches it, whichever tab the driver looks at."""
        steps = [{"action": "click", "element": "Open partner"}, {"action": "type_text", "field": "Word", "value": "^word@1"},
                 {"action": "click", "element": "Send word"}, {"action": "finish"}]
        result = self.start(self.fx_task("popup"), label_script(steps)).run()
        self.assertEqual(SITE.FX_HITS, [])
        self.assertNotEqual(result["attempt_status"], "completed")
        for options in self.offered():
            self.assertFalse({name for op, name in options if op == "switch_tab"} & {"Partner desk"})
            self.assertNotIn(("type_text", "Word"), options)
        self.assertEqual([a for a in result["actions"] if a["operation"] in ("type_text", "switch_tab")], [])

    def test_f05_a_cancel_after_the_commit_keeps_that_action_and_dispatches_nothing_after_it(self):
        """F05, commit then cancel: the cancel arrives while the committed Save is in the driver. The Save's effect is
        recorded, the attempt ends cancelled, and the finish that would follow is never reached."""
        def cancel_inside_save(call, provider):
            if call == 4:      # the decision for Save note
                adapter, run = self.run_.adapter, self.run_
                original = adapter.execute_prepared

                def wrapped(prepared, action_id):
                    if prepared.operation == "click":
                        run.cancel()
                    return original(prepared, action_id)

                adapter.execute_prepared = wrapped

        result = self.start(self.task(), label_script(HAPPY_PATH, before=cancel_inside_save)).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("cancelled", "cancel_requested"))
        self.assertEqual(SITE.NOTES, {"125": ["Перезвонить завтра"]})       # the committed Save did happen
        saves = [a for a in result["actions"] if a["operation"] == "click" and (a.get("request") or {}).get("method") == "POST"]
        self.assertEqual(len(saves), 1)
        self.assertNotEqual(saves[0]["effect"], "not_dispatched")
        self.assertEqual(result["actions"][-1]["id"], saves[0]["id"])        # nothing after it
        self.assertEqual(result["budget"]["dispatches"], 6)

    def test_f08_an_old_success_banner_is_not_evidence_that_the_note_was_saved(self):
        """F08, an old toast: order 303 shows "Note saved" with the very note text and never saves anything. The
        criterion reads the order's notes, not the banner, and fails."""
        steps = [{"action": "type_text", "field": "Order number", "value": "^order@1"}, {"action": "click", "element": "button 'Search'"},
                 {"action": "click", "element": "Заказ №303"}, {"action": "type_text", "field": "textbox 'Note'", "value": "^note@1"},
                 {"action": "click", "element": "Save note"}, {"action": "finish"}]
        slots = [{"id": "order", "version": 1, "value": "303", "sensitivity": "normal"},
                 {"id": "note", "version": 1, "value": "Перезвонить завтра", "sensitivity": "normal"},
                 {"id": "record", "version": 1, "value": "Заказ №303", "sensitivity": "normal"}]
        criteria = [{"id": "note_saved", "predicate": "revisit_match",
                     "args": {"anchor_slot_ref": "record@1", "field_name": "Notes", "expected_slot_ref": "note@1"}}]
        result = self.start(self.task(text_slots=slots, criteria=criteria), label_script(steps)).run()
        self.assertEqual(SITE.NOTES, {})
        rows = {r["id"]: r["status"] for r in result["verification"]}
        self.assertEqual(rows["note_saved"], "fail")
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("failed", "postcondition_failed"))

    def test_f10_a_target_past_the_observation_limit_is_reported_and_handed_off_not_guessed(self):
        """F10: 300 links in one screen, more than one observation lists, so scrolling cannot reach the rest. The
        observation says so, and the attempt hands off rather than acting on something else."""
        result = self.start(self.fx_task("dense"), label_script([{"action": "escalate"}])).run()
        observed = next(r for r in self.trace() if r["kind"] == "observed")["coverage"]
        self.assertEqual(observed["status"], "partial")
        self.assertIn("control_limit", observed["reasons"])
        self.assertGreater(observed["controls_not_offered"], 0)
        self.assertNotIn(("click", "Item 300"), self.offered()[0])
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "needs_handoff"))
        self.assertEqual(result["budget"]["dispatches"], 1)


class StandInDecisions(BaseHTTPRequestHandler):
    """A hand-written planner behind the decisions wire format. It is NOT Jev: it exists so the
    jev path (CLI, key, budget, served model, no hidden retry) can be exercised without the service."""

    requests: list = []
    model = "stand-in/planner-1"

    def log_message(self, *args):
        pass

    @staticmethod
    def plan(state, questions):
        obs = state["observation"]
        text, path = obs["summary"], urlsplit(obs["url"]).path
        if path == "/orders" and "q=" not in obs["url"]:
            if "Order number: empty" in text:
                return {"action": "type_text", "field": "Order number", "value": "^order@1"}
            return {"action": "click", "element": "button 'Search'"}
        if path == "/orders":
            return {"action": "click", "element": "Заказ №125"}
        if path == "/orders/125":
            if "Перезвонить завтра" in text and "Note: empty" in text:
                return {"action": "finish"}
            if "Note: empty" in text:
                return {"action": "type_text", "field": "textbox 'Note'", "value": "^note@1"}
            return {"action": "click", "element": "Save note"}
        return {"action": "finish"}

    def do_POST(self):
        from reflexmesh.adapters.system_one.decision_providers import answers_for_step

        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"auth": self.headers.get("authorization"), "body": body})
        data = json.dumps({"answers": answers_for_step(self.plan(body["state"], body["questions"]), body["questions"]),
                           "model": type(self).model, "id": f"stand-in-{len(type(self).requests)}",
                           "usage": {"input_tokens": 11, "output_tokens": 5}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(AVAILABLE, "set REFLEXMESH_CHROME and install the pinned Browser Use + SystemOneHarness")
class JevPathWithStandIn(BrowserCase):
    KEY = "sk-or-test-0123456789abcdef"

    def setUp(self):
        super().setUp()
        StandInDecisions.requests = []
        self.decisions = ThreadingHTTPServer(("127.0.0.1", 0), StandInDecisions)
        threading.Thread(target=self.decisions.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.decisions.shutdown(), self.decisions.server_close()))

    def cli(self, **limits):
        import argparse
        import contextlib
        import io
        from unittest import mock

        from reflexmesh.runtime.browser_cli import run_browser

        data = task_dict(start_url=self.origin + "/orders", allowed_origins=[self.origin])
        data["limits"] = {**data["limits"], **limits}
        task_file = Path(self.out.name) / "task.json"
        task_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        args = argparse.Namespace(
            input=str(task_file), output_dir=str(Path(self.out.name) / "run"), action_provider="jev", script=None,
            model=None, chrome=CHROME, no_sandbox=not SANDBOX, attempt_id=None,
            decisions_url=f"http://127.0.0.1:{self.decisions.server_port}")
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": self.KEY}), contextlib.redirect_stdout(stdout):
            code = run_browser(args)
        return code, json.loads(stdout.getvalue()), stdout.getvalue()

    def test_jev_path_counts_every_request_and_records_requested_and_served_model(self):
        code, result, raw = self.cli()
        self.assertEqual(code, 4)  # incomplete: no verifier yet
        self.assertEqual(SITE.NOTES, {"125": ["Перезвонить завтра"]})
        requests = StandInDecisions.requests
        self.assertEqual(result["budget"]["model_calls"], len(requests))
        decisions = [r for r in self.trace() if r["kind"] == "decision_received"]
        self.assertEqual(len(decisions), len(requests))
        self.assertEqual(result["decision"]["kind"], "jev")
        self.assertEqual(result["decision"]["requested_model"], "~typesafe/jev-latest")
        self.assertEqual(result["decision"]["served_models"], ["stand-in/planner-1"])
        self.assertTrue(all(d["requested_model"] == "~typesafe/jev-latest" and d["served_model"] == "stand-in/planner-1"
                            for d in decisions))
        self.assertTrue(all(a["producer"] == "jev" for a in result["actions"]))
        self.assertEqual(result["components"]["decision_provider"]["config_id"],
                         f"openrouter:~typesafe/jev-latest@127.0.0.1:{self.decisions.server_port}")

    def test_the_key_goes_to_the_endpoint_and_nowhere_else(self):
        code, result, raw = self.cli()
        self.assertTrue(all(r["auth"] == f"Bearer {self.KEY}" for r in StandInDecisions.requests))
        everything = raw + (Path(self.out.name) / "run" / "trace.jsonl").read_text("utf-8") + \
            (Path(self.out.name) / "run" / "result.json").read_text("utf-8")
        self.assertNotIn(self.KEY, everything)

    def test_the_model_call_budget_stops_the_loop_without_hidden_requests(self):
        code, result, raw = self.cli(max_model_calls=3)
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("incomplete", "model_call_limit"))
        self.assertEqual(len(StandInDecisions.requests), 3)
        self.assertEqual(result["budget"]["model_calls"], 3)

    def test_without_a_key_the_jev_path_is_unavailable_not_silently_scripted(self):
        import argparse
        import contextlib
        import io
        from unittest import mock

        from reflexmesh.runtime.browser_cli import run_browser

        task_file = Path(self.out.name) / "task.json"
        task_file.write_text(json.dumps(task_dict(start_url=self.origin + "/orders", allowed_origins=[self.origin]),
                                        ensure_ascii=False), encoding="utf-8")
        args = argparse.Namespace(input=str(task_file), output_dir=str(Path(self.out.name) / "run"),
                                  action_provider="jev", script=None, model=None, chrome=CHROME,
                                  no_sandbox=not SANDBOX, attempt_id=None, decisions_url=None)
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(stdout):
            code = run_browser(args)
        result = json.loads(stdout.getvalue())
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("blocked", "executor_unavailable"))
        self.assertEqual(result["execution_outcome"], "not_started")
        self.assertEqual(SITE.REQUESTS, [])  # no browser was started


class RefusedBeforeStart(unittest.TestCase):
    """Decided from the task alone: no browser, so these run everywhere."""

    def refuse(self, **changes):
        task = BrowserTask.from_dict(task_dict(**changes))
        return BrowserRun(task, chrome=None, provider_factory=lambda: (None, "x", None), action_provider="script").run()

    def test_unknown_predicate_is_blocked(self):
        result = self.refuse(criteria=[{"id": "x", "predicate": "visual_match", "args": {}}])
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("blocked", "unsupported_criterion"))
        self.assertEqual(result["execution_outcome"], "not_started")

    def test_requested_constraint_is_blocked_not_ignored(self):
        result = self.refuse(constraints=[{"id": "no-buy", "predicate": "never_purchase", "args": {}}])
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("blocked", "unsupported_constraint"))

    def test_missing_executor_is_reported_as_unavailable(self):
        result = self.refuse()
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("blocked", "executor_unavailable"))


if __name__ == "__main__":
    unittest.main()
