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
        self.assertEqual(SITE.NOTES, {"125": ["Перезвонить завтра"]})
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

    def test_a_link_to_another_origin_is_refused_before_any_request(self):
        steps = [{"action": "click", "element": "External portal"}]
        result = self.start(self.task(start="/"), label_script(steps)).run()
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("blocked", "policy_denied"))
        self.assertEqual(result["budget"]["dispatches"], 1)  # only the permitted first navigation
        self.assertFalse([r for r in SITE.REQUESTS if (r.get("host") or "").startswith("localhost")])

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
