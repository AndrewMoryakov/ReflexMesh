"""V0.3 protocol artifacts are consistent and the scorer applies thresholds as written."""

import json
from pathlib import Path
import unittest

from scripts import v03_run, v03_score

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments" / "v03"


class DatasetTests(unittest.TestCase):
    def test_dataset_follows_rubric(self):
        rows = v03_run.load_dataset(EXP / "dataset.jsonl")
        self.assertEqual(len(rows), 60)
        self.assertEqual(len({r["case_id"] for r in rows}), 60)
        kinds = {k: sum(r["kind"] == k for r in rows) for k in ("single", "ambiguous", "refusal", "composite")}
        self.assertEqual(kinds, {"single": 30, "ambiguous": 15, "refusal": 8, "composite": 7})
        for r in rows:
            self.assertTrue(set(r["acceptable_routes"]) <= set(r["allowed_routes"]), r["case_id"])
            self.assertTrue(v03_run.eligible(r), r["case_id"])
            if r["kind"] in ("single", "ambiguous"):
                self.assertTrue(r["acceptable_routes"], r["case_id"])
            else:
                self.assertEqual(r["acceptable_routes"], [], r["case_id"])

    def test_llm_prompt_uses_adapter_descriptions_and_only_eligible(self):
        case = {"goal": "G", "capabilities": ["CUA", "LLM", "PERCEPTION"], "allowed_routes": ["LLM", "CUA"]}
        system, user = v03_run.llm_prompt(case)
        self.assertIn("ABSTAIN", system)
        self.assertIn("Request:\nG", user)
        self.assertIn("id: CUA", user)
        self.assertNotIn("PERCEPTION", user)
        self.assertIn("Which single capability should handle this request?", user)

    def test_parse_llm(self):
        self.assertEqual(v03_run.parse_llm('{"route": "LLM"}', ["LLM"]), ("selected", "LLM", None))
        self.assertEqual(v03_run.parse_llm('```json\n{"route": "ABSTAIN"}\n```', ["LLM"])[0], "abstained")
        self.assertEqual(v03_run.parse_llm('{"route": "CUA"}', ["LLM"]), ("selected", "CUA", "outside_candidates"))
        self.assertEqual(v03_run.parse_llm("LLM", ["LLM"])[0], "error")


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.protocol = json.loads((EXP / "protocol.json").read_text(encoding="utf-8"))
        base = {"capabilities": ["CUA", "LLM", "PERCEPTION"], "allowed_routes": ["LLM", "CUA"]}
        self.dataset = [dict(base, case_id=f"s{i}", kind="single", lang="en", acceptable_routes=["LLM"])
                        for i in range(10)]
        self.dataset.append(dict(base, case_id="r", kind="refusal", lang="en", acceptable_routes=[]))

    def rec(self, comp, cid, route, run=1, status="selected", latency=100.0):
        return {"comparator": comp, "case_id": cid, "run": run, "status": status, "route": route,
                "latency_ms": latency, "cost": 0.001, "calls": 1}

    def test_metrics_and_verdict(self):
        recs = []
        for run in (1, 2, 3):
            recs += [self.rec("jev", f"s{i}", "LLM", run) for i in range(10)]
            recs.append(self.rec("jev", "r", None, run, "abstained"))
            recs += [self.rec("llm", f"s{i}", "LLM", run, latency=500) for i in range(10)]
            recs.append(self.rec("llm", "r", "PERCEPTION", run))
        recs += [self.rec("rules", f"s{i}", "LLM" if i < 5 else None, 1, "selected" if i < 5 else "abstained")
                 for i in range(10)]
        m = v03_score.score(recs, self.dataset, self.protocol)
        self.assertEqual(m["jev"]["accuracy"], 1.0)
        self.assertEqual(m["jev"]["stability"], 1.0)
        self.assertEqual(m["jev"]["refusal_abstention"], 1.0)
        self.assertEqual(m["llm"]["forbidden"], 3)
        self.assertEqual(m["rules"]["accuracy"], 0.5)
        self.assertEqual(v03_score.criteria(m, self.protocol)["verdict"], "useful")

    def test_error_rate_blocks_verdict(self):
        recs = [self.rec("jev", f"s{i}", "LLM") for i in range(9)] + [self.rec("jev", "s9", None, status="error")]
        recs += [self.rec("rules", f"s{i}", None, status="abstained") for i in range(10)]
        recs += [self.rec("llm", f"s{i}", "LLM") for i in range(10)]
        result = v03_score.criteria(v03_score.score(recs, self.dataset, self.protocol), self.protocol)
        self.assertFalse(result["criteria"]["T6"])
        self.assertEqual(result["verdict"], "not_confirmed")


if __name__ == "__main__":
    unittest.main()
