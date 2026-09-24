"""V0.3b harness mirrors the adapter payload and maps variant outcomes correctly."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest

from scripts import v03b_run, v03b_score
from reflexmesh.contracts.task import Task, Route
from reflexmesh.routing.jev_router import route_task

ROOT = Path(__file__).resolve().parents[1]
CASE = {"case_id": "x", "goal": "Нажми «Оплатить»", "capabilities": ["CUA", "LLM", "PERCEPTION"],
        "allowed_routes": ["LLM", "PERCEPTION"], "acceptable_routes": []}


def response(selected, status="selected"):
    return {"status": status, "decision_id": "d", "provenance": {"jev_provider": "p"},
            "decision": {"selected": selected, "jev_choice": selected},
            "raw_jev": {"id": "g", "usage": {"cost": 1e-5}, "answers": {"tool": {"confidence": 0.9}}}}


class HarnessTests(unittest.TestCase):
    def test_j0_payload_equals_adapter_payload(self):
        captured = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(500)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            task = Task("0.1", "x", CASE["goal"], (Route.CUA, Route.LLM, Route.PERCEPTION),
                        (Route.LLM, Route.PERCEPTION))
            route_task(task, endpoint=f"http://127.0.0.1:{server.server_port}", none_candidate=False)
            route_task(task, endpoint=f"http://127.0.0.1:{server.server_port}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(captured[0], v03b_run.payload(CASE, "J0"))
        # ADR-0003: the adapter's default request is exactly the measured J1 request.
        self.assertEqual(captured[1], v03b_run.payload(CASE, "J1"))

    def test_candidate_sets(self):
        self.assertEqual(v03b_run.candidate_ids(CASE, "J0"), ["LLM", "PERCEPTION"])
        self.assertEqual(v03b_run.candidate_ids(CASE, "J1"), ["LLM", "PERCEPTION", "NONE"])
        self.assertEqual(v03b_run.candidate_ids(CASE, "J2"), ["CUA", "LLM", "PERCEPTION"])
        self.assertEqual(v03b_run.candidate_ids(CASE, "J3"), ["CUA", "LLM", "PERCEPTION", "NONE"])

    def test_interpret(self):
        sent = ["CUA", "LLM", "PERCEPTION", "NONE"]
        self.assertEqual(v03b_run.interpret(response("LLM"), CASE, sent)["status"], "selected")
        r = v03b_run.interpret(response("CUA"), CASE, sent)
        self.assertEqual((r["status"], r["route"], r["reason"]), ("abstained", None, "selected_not_allowed"))
        self.assertEqual(v03b_run.interpret(response("NONE"), CASE, sent)["reason"], "none_selected")
        self.assertEqual(v03b_run.interpret(response(None, "no_decision"), CASE, sent)["status"], "abstained")
        self.assertEqual(v03b_run.interpret(response("X"), CASE, sent)["status"], "error")
        bad = dict(response(None, "no_decision"), error={"code": "jev_auth_error"})
        self.assertEqual(v03b_run.interpret(bad, CASE, sent)["status"], "error")


class DatasetTests(unittest.TestCase):
    def test_control_and_old_refusal_are_verbatim_v03(self):
        v03 = {r["case_id"]: r for r in map(json.loads, (ROOT / "experiments/v03/dataset.jsonl").read_text().splitlines())}
        rows = [json.loads(l) for l in (ROOT / "experiments/v03b/dataset.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 84)
        for r in rows:
            if r["source"] == "v03":
                base = {k: v for k, v in r.items() if k not in ("set", "subtype", "source")}
                self.assertEqual(base, v03[r["case_id"]])
            self.assertTrue(set(r["acceptable_routes"]) <= set(r["allowed_routes"]))
            if r["set"] == "refusal":
                self.assertEqual(r["acceptable_routes"], [])
                self.assertIn(r["subtype"], ("disallowed", "out_of_catalog"))
        self.assertEqual(sum(r["set"] == "control" for r in rows), 60)
        self.assertEqual(sum(r["subtype"] == "disallowed" for r in rows), 12)
        self.assertEqual(sum(r["subtype"] == "out_of_catalog" for r in rows), 12)


class ScoreTests(unittest.TestCase):
    def test_recommendation(self):
        protocol = json.loads((ROOT / "experiments/v03b/protocol.json").read_text())
        dataset = [{"case_id": f"c{i}", "set": "control", "acceptable_routes": ["LLM"], "allowed_routes": ["LLM"],
                    "subtype": None} for i in range(4)]
        dataset += [{"case_id": f"r{i}", "set": "refusal", "acceptable_routes": [], "allowed_routes": ["LLM"],
                     "subtype": "disallowed" if i < 2 else "out_of_catalog"} for i in range(4)]
        recs = []
        for comp, refuse in (("J0", False), ("J1", True)):
            for run in (1, 2, 3):
                recs += [{"comparator": comp, "run": run, "case_id": f"c{i}", "status": "selected", "route": "LLM",
                          "latency_ms": 1} for i in range(4)]
                recs += [{"comparator": comp, "run": run, "case_id": f"r{i}",
                          "status": "abstained" if refuse else "selected", "route": None if refuse else "LLM",
                          "latency_ms": 1} for i in range(4)]
        m = v03b_score.score(recs, dataset)
        result = v03b_score.criteria(m, protocol)
        self.assertFalse(result["criteria"]["J0"]["R1"])
        self.assertEqual(result["recommended"], "J1")


if __name__ == "__main__":
    unittest.main()
