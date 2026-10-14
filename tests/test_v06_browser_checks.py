"""Registered checks (V0.6 section 11): the service's registry and the demo order desk's claim protocol."""

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reflexmesh.verification.checks import CheckUnavailable, OrderDeskCheck, load_checks

SPEC = importlib.util.spec_from_file_location("order_site_checks", ROOT / "experiments/v06/site/server.py")
SITE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SITE)


def row(op, method=None, path=None, effect="unknown", **extra):
    return {"operation": op, "effect": effect, "evidence_refs": [],
            "request": {"method": method, "path": path} if method else None, **extra}


class Registry(unittest.TestCase):
    def load(self, value):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(value, f)
        return load_checks(f.name)

    def test_only_known_kinds_with_exact_arguments(self):
        checks = self.load({"checks": {"a": {"kind": "order_desk", "url": "http://127.0.0.1:1"}}})
        self.assertIsInstance(checks["a"], OrderDeskCheck)
        for bad in ({"checks": {"a": {"kind": "shell", "url": "http://127.0.0.1:1"}}},
                    {"checks": {"a": {"kind": "order_desk", "url": "http://127.0.0.1:1", "code": "x"}}},
                    {"checks": {"a": {"kind": "order_desk", "url": "http://example.com"}}},   # plain http off loopback
                    {"other": {}}):
            with self.assertRaises(ValueError, msg=bad):
                self.load(bad)

    def test_the_token_never_shows(self):
        check = OrderDeskCheck("a", "http://127.0.0.1:1")
        check._token = "secret-token-value"
        self.assertNotIn("secret-token-value", repr(check))


class Judgements(unittest.TestCase):
    check = OrderDeskCheck("desk", "http://127.0.0.1:1")

    def evidence(self, requests=(), notes=None, pending=0):
        return {"requests": list(requests), "notes_added": notes or {}, "pending": pending}

    def test_the_criterion_wants_exactly_the_note_once(self):
        judge = lambda notes, pending=0: self.check.judge({"subject": "125"}, ["Перезвонить завтра"],
                                                          self.evidence(notes=notes, pending=pending))
        self.assertEqual(judge({"125": ["Перезвонить завтра"]})["status"], "pass")
        self.assertEqual(judge({})["reason"], "not_saved")
        self.assertEqual(judge({"125": ["Перезвонить завтра"] * 2})["reason"], "saved_otherwise")
        self.assertEqual(judge({"125": ["Перезвонить завтра"]}, pending=1)["status"], "unknown")
        self.assertEqual(self.check.judge({"subject": "125"}, ["x"], None)["status"], "unknown")

    def test_one_post_per_committed_submit_settles_everything(self):
        actions = [row("navigate", effect="applied"), row("type_text"), row("click", "GET", "/orders"),
                   row("click", "POST", "/orders/125/note")]
        verdict = self.check.settle(actions, self.evidence([{"method": "GET", "path": "/orders"},
                                                            {"method": "POST", "path": "/orders/125/note"}]),
                                    browser_stopped=True)
        self.assertEqual(verdict[0], "pass")
        self.assertEqual([a["effect"] for a in actions], ["applied", "not_applied", "not_applied", "applied"])

    def test_more_posts_than_submits_is_a_repeat_even_inside_the_driver(self):
        actions = [row("click", "POST", "/orders/125/note")]
        verdict = self.check.settle(actions, self.evidence([{"method": "POST", "path": "/orders/125/note"}] * 2),
                                    browser_stopped=True)
        self.assertEqual(verdict[0], "fail")
        self.assertEqual(actions[0]["effect"], "unknown")

    def test_a_post_no_submit_accounts_for_decides_nothing(self):
        actions = [row("click"), row("type_text")]          # e.g. a script handler that posts by itself
        verdict = self.check.settle(actions, self.evidence([{"method": "POST", "path": "/orders/125/note"}]),
                                    browser_stopped=True)
        self.assertIsNone(verdict)
        self.assertEqual([a["effect"] for a in actions], ["unknown", "unknown"])

    def test_pending_requests_or_a_live_browser_settle_nothing_as_absent(self):
        actions = [row("click", "POST", "/orders/125/note"), row("type_text")]
        self.assertIsNone(self.check.settle(actions, self.evidence(pending=1), browser_stopped=True))
        self.assertIsNone(self.check.settle(actions, self.evidence(), browser_stopped=False))
        self.assertEqual([a["effect"] for a in actions], ["unknown", "unknown"])


class ClaimProtocol(unittest.TestCase):
    """The demo site's side: a claim binds the record to one attempt; evidence needs the token."""

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
        for store in (SITE.NOTES, SITE.META, SITE.REQUESTS, SITE.CLAIM):
            store.clear()
        SITE.IN_FLIGHT[0] = 0

    def test_claim_evidence_release(self):
        import urllib.request

        SITE.NOTES["125"] = ["old note"]                   # before the claim: not this attempt's
        check = OrderDeskCheck("desk", self.origin)
        check.claim("attempt-1")
        with self.assertRaises(CheckUnavailable) as busy:
            OrderDeskCheck("desk", self.origin).claim("attempt-2")
        self.assertEqual(str(busy.exception), "check_busy")
        body = "note=" + urllib.request.quote("Перезвонить завтра")
        urllib.request.urlopen(urllib.request.Request(self.origin + "/orders/125/note", data=body.encode()))
        evidence = check.evidence()
        self.assertEqual(evidence["notes_added"], {"125": ["Перезвонить завтра"]})
        self.assertIn({"method": "POST", "path": "/orders/125/note"}, evidence["requests"])
        self.assertEqual(evidence["pending"], 0)
        check.release()
        self.assertEqual(SITE.CLAIM, {})

    def test_evidence_needs_the_token(self):
        import urllib.error
        import urllib.request

        OrderDeskCheck("desk", self.origin).claim("attempt-1")
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(self.origin + "/__evidence")
        self.assertEqual(denied.exception.code, 401)
        forged = OrderDeskCheck("desk", self.origin)
        forged._token, forged.claim_id, forged.attempt_id = "guess", "x", "attempt-1"
        with self.assertRaises(CheckUnavailable):
            forged.evidence()


if __name__ == "__main__":
    unittest.main()
