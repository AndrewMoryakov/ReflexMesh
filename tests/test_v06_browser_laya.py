"""The laya translator: an SOH request in laya-browser's training format, and the answers back.

A fake laya server records what is sent and returns canned answers. These tests prove the format
(state keys, verbatim instructions, page-order indices, no markup), the mapping back onto SOH keys,
what the translator fills in itself, and the accounting. They say nothing about how well laya chooses.
"""

import importlib.util
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SOH = importlib.util.find_spec("systemone_harness") is not None

from reflexmesh.adapters.system_one import laya_provider as laya  # noqa: E402

SUMMARY = ("URL: http://127.0.0.1:8770/orders\nTitle: Orders\n[15]<a />\n\tDashboard\n[17]<a />\n\tOrders\nOrders\n"
           "Order number\n|SHADOW(open)|[25]<input id=q name=q type=text />\n[26]<button type=submit />\n\tSearch\n"
           "Set so far: Order number: empty.")


def soh_request(values=None, history=None, url="http://127.0.0.1:8770/orders", summary=SUMMARY, field="(empty)",
                reasons=("more_content_below",)):
    values = values or {"order@1": "order@1: 125", "note@1": "note@1: Call back tomorrow"}
    state = {"goal": "Find order 125 and add the prepared note.", "instructions": "x",
             "observation": {"summary": summary, "url": url, "title": "Orders", "controls": 4, "tabs": 1,
                             "coverage": {"status": "partial", "reasons": list(reasons)}}}
    if history is not None:
        state["history"] = history
    questions = {
        "next_action": {"type": "choice", "instructions": "x", "criteria": {
            "click": "c", "type_text": "t", "select_option": "s", "scroll": "s", "go_back": "b", "switch_tab": "t",
            "wait": "w", "finish": "f", "escalate": "e"}},
        "goal_reached": {"type": "noul", "instructions": "x"},
        "click__element": {"type": "choice", "instructions": "x", "criteria": {
            "15": "link 'Dashboard' → /", "17": "link 'Orders' → /orders", "26": "button 'Search'"}},
        "type_text__field": {"type": "choice", "instructions": "x", "criteria": {"25": f"textbox 'Order number' {field}"}},
        "type_text__value": {"type": "choice", "instructions": "x", "criteria": values},
        "select_option__option": {"type": "choice", "instructions": "x", "criteria": {
            "30:Normal": "Priority: Normal (selected)", "30:Rush": "Priority: Rush"}},
        "scroll__direction": {"type": "choice", "instructions": "x", "criteria": {"down": "d", "up": "u"}},
        "go_back__to": {"type": "choice", "instructions": "x", "criteria": {"back": "/"}},
        "switch_tab__tab": {"type": "choice", "instructions": "x", "criteria": {"ab12": "Other"}},
    }
    return state, questions


def laya_answers(op="TYPE_TEXT", click="3", field="2", select="4:1", confidence=0.8):
    ops = ["CLICK", "TYPE_TEXT", "SELECT", "SCROLL_DOWN", "SCROLL_UP", "WAIT", "DONE", "BLOCKED"]
    probs = {k: (0.6 if k == op else 0.05) for k in ops}
    return {"operation": {"type": "choice", "choice": op, "confidence": confidence, "probabilities": probs},
            "click_target": {"type": "choice", "choice": click, "confidence": 0.9,
                             "probabilities": {"1": 0.05, "2": 0.05, "3": 0.9}},
            "type_text_target": {"type": "choice", "choice": field, "confidence": 1.0, "probabilities": {"2": 1.0}},
            "select_target": {"type": "choice", "choice": select, "confidence": 0.7,
                              "probabilities": {"4:1": 1.0}}}


class Format(unittest.TestCase):
    def test_state_has_the_three_keys_laya_reads_and_no_markup(self):
        t = laya.Translation(*soh_request(), [])
        self.assertEqual(set(t.state), {"page", "elements", "recent_actions"})
        text = t.state["page"]["text"]
        for markup in ("[25]", "<input", "SHADOW", "URL:", "Title:", "Set so far"):
            self.assertNotIn(markup, text)
        self.assertIn("Order number", text)
        self.assertEqual(t.state["page"]["url"], "http://127.0.0.1:8770/orders")

    def test_instructions_are_the_training_texts_verbatim(self):
        t = laya.Translation(*soh_request(), [])
        self.assertEqual(t.questions["operation"]["instructions"]["rules"], laya.NEXT_ACTION)
        self.assertEqual(t.questions["click_target"]["instructions"]["rules"], [laya.NEXT_ACTION, laya.TARGET])
        self.assertEqual(t.questions["operation"]["criteria"]["TYPE_TEXT"], laya.LABELS["TYPE_TEXT"])
        self.assertTrue(laya.NEXT_ACTION.startswith("Advance the user's entire goal from the CURRENT page"))
        self.assertTrue(laya.NEXT_ACTION.endswith("BLOCKED means no supported operation can make progress."))

    def test_elements_are_numbered_from_one_in_page_order_without_link_destinations(self):
        t = laya.Translation(*soh_request(), [])
        labels = [(e["index"], e["label"], e["operations"]) for e in t.state["elements"]]
        self.assertEqual(labels, [("1", "Dashboard", ["CLICK"]), ("2", "Orders", ["CLICK"]),
                                  ("3", "Order number", ["TYPE_TEXT"]), ("4", "Search", ["CLICK"]),
                                  ("5", "Priority", ["SELECT"])])
        self.assertEqual(t.back["TYPE_TEXT"], {"3": "25"})
        self.assertEqual(t.back["SELECT"], {"5:1": "30:Rush"})      # the chosen option is the value, not a target
        self.assertEqual(t.questions["select_target"]["criteria"]["5:1"]["current_value"], "Normal")
        criteria = json.dumps(t.questions, ensure_ascii=False)
        self.assertNotIn("→ /", criteria)
        self.assertEqual(t.state["elements"][4]["value"], "Normal")

    def test_field_value_and_checked_state_reach_the_model(self):
        t = laya.Translation(*soh_request(field="(filled: '125')"), [])
        field = next(e for e in t.state["elements"] if e["label"] == "Order number")
        self.assertEqual(field["value"], "125")
        self.assertEqual(laya.parse_label("checkbox 'Rush' (checked)")["checked"], "true")
        self.assertEqual(laya.parse_label("checkbox 'Rush' (unchecked)")["checked"], "false")
        self.assertEqual(laya.parse_label('link "It\'s here" → /x')["name"], "It's here")

    def test_page_controls_are_the_trained_ones_and_follow_the_scroll_position(self):
        t = laya.Translation(*soh_request(), [])
        ops = t.questions["operation"]["criteria"]
        self.assertEqual(ops["SCROLL_DOWN"], "Scroll down")
        self.assertEqual(ops["WAIT"], "Wait for the page to update")
        self.assertNotIn("SCROLL_UP", ops)
        self.assertNotIn("GO_BACK", ops)
        t = laya.Translation(*soh_request(reasons=("more_content_above",)), [])
        self.assertIn("SCROLL_UP", t.questions["operation"]["criteria"])
        self.assertNotIn("SCROLL_DOWN", t.questions["operation"]["criteria"])

    def test_only_offered_operations_and_no_switch_tab(self):
        state, questions = soh_request()
        questions["next_action"]["criteria"] = {"click": "c", "finish": "f", "escalate": "e"}
        t = laya.Translation(state, questions, [])
        self.assertEqual(set(t.questions["operation"]["criteria"]), {"CLICK", "DONE", "BLOCKED"})
        self.assertEqual(set(t.questions), {"operation", "click_target"})


class AnswersBack(unittest.TestCase):
    def back(self, op, **kw):
        state, questions = soh_request(**{k: v for k, v in kw.items() if k in ("values", "field")})
        t = laya.Translation(state, questions, [])
        mapped = {"CLICK": "4", "TYPE_TEXT": "3", "SELECT": "5:1"}
        answers = laya_answers(op, click=mapped["CLICK"], field=mapped["TYPE_TEXT"], select=mapped["SELECT"])
        answers["click_target"]["probabilities"] = {"1": 0.05, "2": 0.05, "4": 0.9}
        answers["type_text_target"]["probabilities"] = {"3": 1.0}
        answers["select_target"]["probabilities"] = {"5:1": 1.0}
        return t.answers(answers), questions

    def test_targets_map_back_to_the_soh_keys(self):
        (answers, record, _), _ = self.back("CLICK")
        self.assertEqual(answers["next_action"]["choice"], "click")
        self.assertEqual(answers["click__element"]["choice"], "26")
        self.assertEqual(answers["type_text__field"]["choice"], "25")
        self.assertEqual(answers["select_option__option"]["choice"], "30:Rush")
        self.assertEqual(answers["next_action"]["confidence"], 0.8)
        self.assertEqual(record["operation"], "CLICK")

    def test_scroll_control_maps_to_scroll_with_its_direction(self):
        (answers, record, _), _ = self.back("SCROLL_DOWN")
        self.assertEqual(answers["next_action"]["choice"], "scroll")
        self.assertEqual(answers["scroll__direction"]["choice"], "down")
        self.assertEqual(record["derived"]["scroll__direction"], "laya_control")
        self.assertEqual(record["not_offered"], ["go_back", "switch_tab"])

    def test_done_and_blocked_are_finish_and_escalate(self):
        (answers, _, _), _ = self.back("DONE")
        self.assertEqual(answers["next_action"]["choice"], "finish")
        self.assertEqual(answers["goal_reached"]["noul"], 0.6)
        (answers, _, _), _ = self.back("BLOCKED")
        self.assertEqual(answers["next_action"]["choice"], "escalate")

    def test_slot_value_is_marked_as_the_translators_not_the_models(self):
        (answers, record, _), _ = self.back("TYPE_TEXT", values={"order@1": "order@1: 125"})
        self.assertEqual(answers["type_text__value"]["choice"], "order@1")
        self.assertEqual(record["derived"]["type_text__value"], "single_slot")
        (answers, record, _), _ = self.back("TYPE_TEXT")
        self.assertEqual(answers["type_text__value"]["choice"], "order@1")
        self.assertEqual(record["derived"]["type_text__value"], "reference_matches_field_name")

    def test_an_unresolved_slot_has_probability_zero_so_the_gate_refuses(self):
        (answers, record, _), questions = self.back("TYPE_TEXT", values={"a@1": "a@1: x", "b@1": "b@1: y"})
        self.assertEqual(record["derived"]["type_text__value"], "unresolved")
        self.assertEqual(set(answers["type_text__value"]["probabilities"].values()), {0.0})

    def test_an_operation_that_was_not_offered_is_an_error(self):
        t = laya.Translation(*soh_request(), [])
        with self.assertRaises(ValueError):
            t.answers(laya_answers("PRESS_ENTER"))

    @unittest.skipUnless(SOH, "systemone_harness is not installed")
    def test_mapped_answers_pass_the_validator_and_the_gate(self):
        from systemone_harness.actions import ActionSpace
        from systemone_harness.gate import Gate
        from reflexmesh.adapters.system_one.browser_adapter import browser_action_space
        from reflexmesh.adapters.system_one.decision_providers import ValidatingProvider

        state, questions = soh_request(values={"order@1": "order@1: 125"})
        space = browser_action_space({"click", "type_text", "select_option", "scroll", "go_back", "switch_tab", "wait"})
        candidates = {"elements": questions["click__element"]["criteria"],
                      "text_fields": questions["type_text__field"]["criteria"],
                      "text_values": questions["type_text__value"]["criteria"],
                      "options": questions["select_option__option"]["criteria"],
                      "history": {"back": "/"}, "tabs": {"ab12": "Other"}}
        compiled = space.compile(candidates)
        self.assertIsInstance(space, ActionSpace)
        for op in ("CLICK", "TYPE_TEXT", "SELECT", "SCROLL_DOWN", "WAIT", "DONE"):
            t = laya.Translation(state, compiled.questions, [])
            answers, _, _ = t.answers(laya_answers(op, click="4", field="3", select="5:1", confidence=0.95))
            ValidatingProvider._validate(answers, compiled.questions)
            verdict = Gate(space.gate).judge(answers, compiled, space)
            self.assertIn(verdict.kind, ("run", "finish"), op)


class LayaHandler(BaseHTTPRequestHandler):
    requests: list = []
    reply: dict = {}

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"path": self.path, "body": body})
        data = json.dumps({"answers": type(self).reply, "model": "laya-browser",
                           "usage": {"input_tokens": 0, "output_tokens": 0}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(SOH, "systemone_harness is not installed")
class Wire(unittest.TestCase):
    def setUp(self):
        LayaHandler.requests, LayaHandler.reply = [], laya_answers("TYPE_TEXT", field="3")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LayaHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.provider, self.config = laya.laya_provider(f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_one_request_per_decision_to_the_systemone_path_with_retries_off(self):
        decision = self.provider.decide(*soh_request())
        self.assertEqual(len(LayaHandler.requests), 1)
        self.assertEqual(LayaHandler.requests[0]["path"], "/v1/systemone")
        self.assertEqual(self.provider.wire.retries, 0)
        self.assertEqual(decision.model, "laya-browser")
        self.assertTrue(self.config.startswith("laya:laya-browser@127.0.0.1:"))
        self.assertIn("translated", decision.raw)
        self.assertEqual(set(LayaHandler.requests[0]["body"]["state"]), {"page", "elements", "recent_actions"})

    def test_a_secret_slot_reaches_the_history_only_as_its_placeholder(self):
        values = {"pin@1": "pin@1: (secret value, not shown)"}
        self.provider.decide(*soh_request(values=values, history=[]))
        self.provider.decide(*soh_request(values=values, history=["type_text(field=25, value=pin@1) -> typed"]))
        body = LayaHandler.requests[-1]["body"]
        self.assertEqual(body["state"]["recent_actions"][0]["text"], "(secret value, not shown)")
        self.assertNotIn("pin@1", json.dumps(body))     # not even the reference: laya only sees what was typed

    def test_recent_actions_record_what_ran_and_whether_the_page_changed(self):
        self.provider.decide(*soh_request(history=[]))
        self.assertEqual(LayaHandler.requests[-1]["body"]["state"]["recent_actions"], [])
        self.provider.decide(*soh_request(history=["type_text(field=25, value=order@1) -> typed"],
                                          field="(filled: '125')", summary=SUMMARY + "\nchanged"))
        recent = LayaHandler.requests[-1]["body"]["state"]["recent_actions"]
        self.assertEqual(recent, [{"action": "Order number", "kind": "fill", "text": "125", "page_changed": True}])

    def test_renumbered_elements_alone_are_not_a_page_change(self):
        self.provider.decide(*soh_request(history=[]))
        renumbered = SUMMARY.replace("[25]", "[62]").replace("[26]", "*[63]")
        self.provider.decide(*soh_request(history=["click(element=26) -> clicked"], summary=renumbered))
        self.assertIs(LayaHandler.requests[-1]["body"]["state"]["recent_actions"][0]["page_changed"], False)
        self.assertNotIn("*", LayaHandler.requests[-1]["body"]["state"]["page"]["text"])

    def test_a_refused_proposal_is_not_history(self):
        self.provider.decide(*soh_request(history=[]))
        self.provider.decide(*soh_request(history=["refused type_text(field=25): weakest judgment 0.60 below 0.70"]))
        self.assertEqual(LayaHandler.requests[-1]["body"]["state"]["recent_actions"], [])

    def test_the_server_endpoint_is_checked_and_required(self):
        from reflexmesh.adapters.system_one.decision_providers import ProviderUnavailable

        for bad in (None, "", "http://100.96.1.1:8791", "https://x.test/v1/systemone"):
            with self.assertRaises(ProviderUnavailable, msg=bad):
                laya.laya_provider(bad)


if __name__ == "__main__":
    unittest.main()
