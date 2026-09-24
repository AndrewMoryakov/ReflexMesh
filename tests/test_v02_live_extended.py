"""The extended live collector links every request to one receipt and never fakes a pass."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import collect_v02_live_extended as extended

base = extended.base


def run_suite(suite: str, fail: bool = False) -> tuple[int, dict, list]:
    with tempfile.TemporaryDirectory() as directory:
        decisions = Path(directory) / "upstream" / ".jevrouter" / "decisions"
        decisions.mkdir(parents=True)
        evidence = Path(directory) / "evidence"
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "provider": "typesafe"}).encode())

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                ids = [candidate["id"] for candidate in request["candidates"]]
                calls.append(ids)
                decision_id = f"dec_{len(calls)}"
                selected = None if fail else ids[0]
                result = {
                    "request_id": f"req_{len(calls)}", "decision_id": decision_id,
                    "mode": "decision_only", "status": "no_decision" if fail else "selected",
                    "execution": {"enabled": False, "status": "not_started"},
                    "provenance": {"jev_provider": "typesafe"},
                    "decision": {"kind": "choice", "selected": selected, "jev_choice": selected,
                                 "candidates": [
                                     {"id": candidate, "jev_probability": 1.0 if candidate == selected else 0.0,
                                      "jev_confidence": 0.9,
                                      "router": {"available": True, "allowed": True, "filtered": False,
                                                 "requires_confirmation": False}}
                                     for candidate in ids]},
                    "fallback": {"type": None, "reason": None},
                    "raw_jev": {"model": "m", "id": f"gen_{len(calls)}", "usage": {"cost": 0.001},
                                "answers": {"tool": {"confidence": 0.9}}},
                    "error": {"code": "jev_auth_error", "message": "x"} if fail else None,
                }
                body = (json.dumps(result, indent=2) + "\n").encode()
                (decisions / f"{decision_id}.json").write_bytes(body)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        argv = ["collect_v02_live_extended.py", "--upstream-dir", str(decisions.parents[1]),
                "--provider", "typesafe", "--output-dir", str(evidence), "--suite", suite,
                "--jev-url", f"http://127.0.0.1:{server.server_port}"]
        try:
            with patch.object(sys, "argv", argv), patch.object(base, "git", return_value=base.PIN), \
                 patch.object(base.subprocess, "check_output", return_value="v24"):
                code = extended.main()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        report = json.loads((evidence / "report.json").read_text(encoding="utf-8"))
        return code, report, calls


class ExtendedLiveTests(unittest.TestCase):
    def test_main_suite_links_receipts_and_repeats(self):
        code, report, calls = run_suite("main")
        self.assertEqual(code, 0)
        self.assertTrue(report["mechanical_checks_passed"])
        self.assertFalse(report["live_gate_closed"])
        self.assertEqual(len(calls), 13)
        self.assertIn(["CUA", "PERCEPTION", "NONE"], calls)
        self.assertEqual(report["repeat_groups"]["repeat_llm_ru"]["repeats"], 5)
        self.assertTrue(report["repeat_groups"]["repeat_llm_ru"]["stable"])
        # First candidate is always CUA here, so hints mismatch without failing mechanics.
        self.assertFalse(report["cases"]["llm_ru_all"]["observation"]["matches_hint"])
        self.assertAlmostEqual(report["total_cost"], 0.013)

    def test_provider_error_suite_requires_failed_without_fallback(self):
        code, report, calls = run_suite("provider-error", fail=True)
        self.assertEqual(code, 0)
        case = report["cases"]["provider_error"]
        self.assertTrue(case["checks"]["failed_as_provider_error"])
        self.assertEqual(case["observation"]["upstream_error_code"], "jev_auth_error")

    def test_success_is_not_accepted_as_provider_error(self):
        code, report, _ = run_suite("provider-error", fail=False)
        self.assertEqual(code, 1)
        self.assertFalse(report["cases"]["provider_error"]["checks"]["failed_as_provider_error"])


if __name__ == "__main__":
    unittest.main()
