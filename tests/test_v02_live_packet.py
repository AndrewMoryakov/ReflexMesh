"""The evidence collector must link receipts and never certify a mock as live."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import collect_v02_live as collector


class LivePacketTests(unittest.TestCase):
    def test_two_requests_receipts_and_local_empty_set(self):
        with tempfile.TemporaryDirectory() as directory:
            upstream = Path(directory) / "upstream"
            decisions = upstream / ".jevrouter" / "decisions"
            decisions.mkdir(parents=True)
            evidence = Path(directory) / "evidence"
            calls = []

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    body = json.dumps({"ok": True, "provider": "typesafe"}).encode()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(body)

                def do_POST(self):
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    ids = [candidate["id"] for candidate in request["candidates"]]
                    calls.append(ids)
                    decision_id = f"dec_{len(calls)}"
                    result = {
                        "request_id": f"req_{len(calls)}", "decision_id": decision_id,
                        "mode": "decision_only", "status": "selected",
                        "execution": {"enabled": False, "status": "not_started"},
                        "provenance": {"jev_provider": "typesafe"},
                        "decision": {"kind": "choice", "selected": "LLM", "jev_choice": "LLM",
                                     "candidates": [
                                         {"id": candidate, "jev_probability": 0.8 if candidate == "LLM" else 0.1,
                                          "jev_confidence": 0.8,
                                          "router": {"available": True, "allowed": True, "filtered": False,
                                                     "requires_confirmation": False}}
                                         for candidate in ids]},
                        "fallback": {"type": None, "reason": None},
                        "raw_jev": {"answers": {"tool": {"confidence": 0.8}}},
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
            argv = ["collect_v02_live.py", "--upstream-dir", str(upstream),
                    "--provider", "typesafe", "--output-dir", str(evidence),
                    "--jev-url", f"http://127.0.0.1:{server.server_port}"]
            try:
                with patch.object(sys, "argv", argv), patch.object(collector, "git", return_value=collector.PIN), \
                     patch.object(collector.subprocess, "check_output", return_value="v24"):
                    self.assertEqual(collector.main(), 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

            report = json.loads((evidence / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(report["mechanical_checks_passed"])
            self.assertFalse(report["live_gate_closed"])
            self.assertEqual(calls, [["CUA", "LLM", "PERCEPTION", "NONE"], ["LLM", "PERCEPTION", "NONE"]])
            self.assertTrue(report["cases"]["empty"]["checks"]["empty_is_local_abstention"])
            selected = json.loads((evidence / "multi.stdout.json").read_text(encoding="utf-8"))
            (evidence / "decisions" / "dec_1.json").write_bytes(b"altered receipt")
            checked = collector.checked_receipt(selected, evidence / "decisions", evidence,
                                                set(collector.ROUTES))
            self.assertFalse(checked["response_hash_matches"])


if __name__ == "__main__":
    unittest.main()
