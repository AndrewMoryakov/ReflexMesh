"""The read-only run view: how it derives decisions and timings from the journal, and how it serves them."""

import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reflexmesh.runtime.browser_view import build_view, make_server, read_run
from reflexmesh.runtime.browser_view_page import PAGE

EVIDENCE = ROOT / "docs/research/evidence/V0.6-M1-script-run-2026-10-11"


def rec(seq, t, kind, **data):
    return {"seq": seq, "t": t, "kind": kind, **data}


def one_step(base_seq=1, t0=1.0, decision_seconds=0.5, verdict="run", outcome="dispatch"):
    """observed -> options -> request -> answer (+ judged, proposal, then the chosen outcome)."""
    s, t = base_seq, t0
    rows = [rec(s, t, "observed", observation_id=1, page="http://h/p", coverage={"status": "complete", "reasons": []},
                text_chars=1, options={}, page_signature="x"),
            rec(s + 1, t, "options_built", observation_id=1,
                options=[{"ref": "1:elements:26", "operation": "click", "role": "button", "name": "Search"},
                         {"ref": "1:elements:27", "operation": "click", "role": "link", "name": "Other"}]),
            rec(s + 2, t, "decision_requested", observation_id=1, requested_model="m", questions=1, options={}),
            rec(s + 3, t + decision_seconds, "decision_received", observation_id=1, requested_model="m",
                served_model="served/x", request_id="r1", latency_ms=420, usage={"input_tokens": 9, "output_tokens": 2},
                action="click", params={"element": "26"}, confidence=0.91, goal_reached=0.1),
            rec(s + 4, t + decision_seconds, "threshold_judged", action="click", verdict=verdict, weakest=0.91,
                threshold=0.7, confidence=0.91, params={"element": "26"}),
            rec(s + 5, t + decision_seconds + 0.01, "proposal", action="click")]
    if outcome == "dispatch":
        rows += [rec(s + 6, t + decision_seconds + 0.06, "dispatch_committed", action_id=2, operation="click",
                     risk="write", option_ref="1:elements:26", observation_id=1, gate_seq=2),
                 rec(s + 7, t + decision_seconds + 0.46, "driver_returned", action_id=2, ok=True, error=False)]
    elif outcome == "stale":
        rows += [rec(s + 6, t + decision_seconds + 0.03, "admission_rejected", reason="stale_target"),
                 rec(s + 7, t + decision_seconds + 0.03, "reobserve", reason="stale_target")]
    return rows


class Derivation(unittest.TestCase):
    def test_a_decision_is_attributed_labelled_and_timed(self):
        records = [rec(1, 0.0, "attempt_started", attempt_id="a", provider="jev", config_id="openrouter:m",
                       requested_model="m")] + one_step()
        view = build_view(records, None)
        step = view["steps"][0]
        self.assertEqual(step["decision"]["author"], "served/x")
        self.assertEqual(step["decision"]["requested_model"], "m")
        self.assertEqual(step["decision_ms"], 500)
        self.assertEqual(step["decision"]["provider_ms"], 420)
        self.assertEqual(step["chosen"], [{"param": "element", "value": "26", "label": "button 'Search'"}])
        self.assertEqual(step["admission_ms"], 50)
        self.assertEqual(step["driver_ms"], 400)
        self.assertEqual(step["outcome"], {"kind": "done"})
        self.assertEqual(view["summary"]["authors"], {"served/x": 1})
        self.assertEqual(view["meta"]["config_id"], "openrouter:m")

    def test_answers_a_translator_filled_in_are_not_attributed_to_the_model(self):
        records, result = read_run(ROOT / "docs/research/evidence/V0.6-laya-run-2026-10-13")
        view = build_view(records, result)
        first = view["steps"][0]
        self.assertEqual(first["decision"]["author"], "laya-browser")
        chosen = {c["param"]: c for c in first["chosen"]}
        self.assertEqual(chosen["value"]["by"], "translator")
        self.assertEqual(chosen["value"]["how"], "reference_matches_field_name")
        self.assertNotIn("by", chosen["field"])
        self.assertEqual(first["decision"]["filled_by_translator"]["goal_reached"], "laya_p_done")
        self.assertIn("выбрал переходник, не модель", PAGE)

    def test_a_stale_choice_is_shown_as_rejected_with_its_admission_time(self):
        view = build_view(one_step(outcome="stale"), None)
        step = view["steps"][0]
        self.assertEqual(step["outcome"], {"kind": "rejected", "reason": "stale_target"})
        self.assertEqual(step["admission_ms"], 20)  # proposal at +0.01 s, rejection at +0.03 s
        self.assertNotIn("dispatch", step)

    def test_a_refused_decision_and_a_failed_request_are_visible(self):
        refused = build_view(one_step(verdict="refused", outcome="none"), None)["steps"][0]
        self.assertEqual(refused["outcome"], {"kind": "refused"})
        rows = one_step(outcome="none")[:3] + [rec(4, 2.0, "decision_failed", error="ProviderError", status=503,
                                                   protocol=False)]
        failed = build_view(rows, None)["steps"][0]
        self.assertEqual(failed["decision_error"]["status"], 503)
        self.assertEqual(failed["outcome"], {"kind": "decision_failed"})
        self.assertEqual(failed["decision_ms"], 1000)

    def test_state_follows_the_journal(self):
        self.assertEqual(build_view([], None)["state"], "waiting")
        running = build_view(one_step()[:2], None)
        self.assertEqual(running["state"], "running")
        self.assertEqual(running["steps"][0]["kind"], "awaiting_decision")
        done = build_view(one_step() + [rec(20, 5.0, "terminal", attempt_status="incomplete",
                                            stop_reason="verification_unknown", handoff_reason=None)], None)
        self.assertEqual(done["state"], "finished")
        self.assertEqual(done["meta"]["total_ms"], 5000)

    def test_summary_statistics(self):
        rows, seq = [], 1
        for i, seconds in enumerate((0.1, 0.2, 0.3, 0.4, 1.0)):
            rows += one_step(seq, 10.0 * (i + 1), seconds, outcome="none")
            seq += 10
        rows.append(rec(seq, 60.0, "terminal", attempt_status="incomplete", stop_reason="x", handoff_reason=None))
        s = build_view(rows, None)["summary"]
        self.assertEqual((s["decisions"], s["decision_ms_total"], s["decision_ms_median"]), (5, 2000, 300))
        self.assertEqual((s["decision_ms_p95"], s["decision_ms_max"]), (1000, 1000))
        self.assertAlmostEqual(s["decision_share"], 2000 / 60000, places=3)

    def test_the_first_navigation_is_timed_apart_from_the_steps(self):
        rows = [rec(1, 0.1, "dispatch_committed", action_id=1, operation="navigate", bootstrap=True,
                    destination="http://h/"), rec(2, 3.1, "driver_returned", action_id=1, ok=True, error=False)]
        rows += one_step(3, 3.2)
        view = build_view(rows, None)
        self.assertEqual(view["meta"]["start"]["driver_ms"], 3000)
        self.assertEqual(len(view["steps"]), 1)  # the navigation is not a decision step

    def test_the_stored_real_run_reads_back(self):
        records, result = read_run(EVIDENCE)
        view = build_view(records, result)
        decided = [s for s in view["steps"] if s.get("decision")]
        self.assertEqual(len(decided), 6)
        self.assertEqual(view["summary"]["authors"], {"script/label": 6})
        self.assertTrue(all(s["decision_ms"] >= 0 for s in decided))
        labels = [c["label"] for s in decided for c in s["chosen"] if c["label"]]
        self.assertIn("button 'Search'", labels)
        self.assertEqual(view["steps"][-1]["kind"], "final_observation")
        self.assertEqual(view["state"], "finished")
        self.assertEqual(view["result"]["stop_reason"], "verification_unknown")
        self.assertIsNotNone(view["meta"]["start"]["driver_ms"])

    def test_a_half_written_last_line_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "trace.jsonl").write_text(json.dumps(rec(1, 0.0, "attempt_started")) + '\n{"seq": 2, "t"',
                                                      encoding="utf-8")
            records, result = read_run(Path(directory))
        self.assertEqual(len(records), 1)
        self.assertIsNone(result)


class Serving(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        for name in ("trace.jsonl", "result.json"):
            (Path(cls.dir.name) / name).write_bytes((EVIDENCE / name).read_bytes())
        cls.server = make_server(Path(cls.dir.name), 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.dir.cleanup()

    def get(self, path, headers=None, method="GET"):
        request = urllib.request.Request(self.base + path, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_page_and_data(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        nonce = re.search(r"nonce-([\w-]+)", headers["Content-Security-Policy"]).group(1)
        self.assertIn(f'nonce="{nonce}"', body.decode())
        self.assertNotIn("__NONCE__", body.decode())
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, headers, body = self.get("/api/run")
        data = json.loads(body)
        self.assertEqual((status, data["state"], data["summary"]["decisions"]), (200, "finished", 6))
        self.assertIn("script-src 'none'", self.get("/api/run")[1]["Content-Security-Policy"])

    def test_nonce_is_fresh_per_response(self):
        nonces = {re.search(r"nonce-([\w-]+)", self.get("/")[1]["Content-Security-Policy"]).group(1) for _ in range(3)}
        self.assertEqual(len(nonces), 3)

    def test_other_hosts_methods_and_paths_are_refused(self):
        self.assertEqual(self.get("/api/run", {"Host": "evil.example"})[0], 403)
        self.assertEqual(self.get("/api/run", {"Host": f"localhost:{self.server.server_port}"})[0], 200)
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.get("/api/run", method=method)[0], 405)
        for path in ("/result.json", "/trace.jsonl", "/../etc/passwd", "/api/run/../x", "/%2e%2e/x"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_listens_on_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")


class PageSafety(unittest.TestCase):
    def test_the_page_never_turns_run_data_into_markup(self):
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(forbidden, PAGE)
        self.assertNotRegex(PAGE, r"(src|href)\s*=\s*[\"']https?:")
        self.assertNotIn("@import", PAGE)

    @unittest.skipUnless(importlib.util.find_spec("playwright") and os.environ.get("REFLEXMESH_CHROME")
                         and Path(os.environ.get("REFLEXMESH_CHROME", "")).exists(),
                         "needs playwright and REFLEXMESH_CHROME")
    def test_a_hostile_option_name_is_rendered_as_text(self):
        from playwright.sync_api import sync_playwright

        evil = "<img src=x onerror=\"document.title='pwned'\"><script>document.title='pwned'</script>"
        rows = one_step(outcome="dispatch")
        rows[1]["options"][0]["name"] = evil
        rows += [rec(30, 3.0, "terminal", attempt_status="incomplete", stop_reason="x", handoff_reason=None)]
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "trace.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            server = make_server(Path(directory), 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path=os.environ["REFLEXMESH_CHROME"], args=["--no-sandbox"])
                    page = browser.new_page()
                    dialogs = []
                    page.on("dialog", lambda d: dialogs.append(d.message))
                    page.goto(f"http://127.0.0.1:{server.server_port}/")
                    page.wait_for_selector("tr.step")
                    page.locator("tr.step").first.click()
                    page.wait_for_selector("tr.detail")
                    self.assertIn(evil, page.locator("tr.detail").inner_text())
                    self.assertEqual(page.locator("tr.step img, tr.detail img").count(), 0)
                    self.assertNotEqual(page.title(), "pwned")
                    self.assertEqual(dialogs, [])
                    browser.close()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
