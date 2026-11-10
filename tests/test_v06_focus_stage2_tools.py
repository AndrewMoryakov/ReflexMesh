"""The stage 2 replay and scorer (experiments/v06/focus_region) keep their two promises.

The replay must not send requests once its spending cap is reached, and the scorer must not count as a gate passage
an answer the live path would refuse (a chosen action whose required target or value was left out or not offered).
No network: the replay's `post` is replaced.
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "experiments/v06/focus_region" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REPLAY, SCORE = load("stage2_replay"), load("stage2_score")

QUESTIONS = {"next_action": {"type": "choice", "criteria": {"click": "press", "scroll": "scroll", "finish": "done"}},
             "click__element": {"type": "choice", "criteria": {"e1": "Save", "e2": "Cancel"}},
             "scroll__direction": {"type": "choice", "criteria": {"up": "up", "down": "down"}}}


def row(seq=0):
    return {"task_id": f"t{seq}", "step": seq, "state": "page", "questions": QUESTIONS,
            "gold": {"next_action": ["click"], "click__element": ["e1"]}}


def answers(action="click", element="e1", confidence=0.99):
    out = {"next_action": {"choice": action, "confidence": confidence}}
    if element is not None:
        out["click__element"] = {"choice": element, "probabilities": {element: 0.95}}
    return out


class ReplayBudget(unittest.TestCase):
    def replay(self, rows, budget, workers=1, before=None, cost=1.0):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        (directory / "steps").mkdir()
        (directory / "steps" / "a.steps.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", "utf-8")
        out = directory / "out"
        out.mkdir()
        if before:
            before(out)
        sent = []

        def post(key, body):
            sent.append(body)
            return 200, {"answers": answers(), "usage": {"cost": cost}, "model": "m"}, 5

        argv = ["stage2_replay", "--steps", str(directory / "steps"), "--out", str(out), "--runs", "B1",
                "--budget", str(budget), "--workers", str(workers)]
        with patch.object(REPLAY, "post", post), patch.object(sys, "argv", argv), \
                patch.object(sys, "stdin", io.StringIO("key\n")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(REPLAY.main(), 0)
        lines = (out / "B1.jsonl").read_text("utf-8").splitlines() if (out / "B1.jsonl").exists() else []
        return sent, [json.loads(line) for line in lines]

    def test_a_pass_that_costs_more_than_the_budget_stops_sending(self):
        """Ten steps at $1 each with a $0.10 budget: the first request spends it, the other nine are not sent."""
        sent, written = self.replay([row(i) for i in range(10)], budget=0.1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(written), 1)

    def test_parallel_workers_overshoot_by_at_most_the_requests_in_flight(self):
        sent, _ = self.replay([row(i) for i in range(10)], budget=0.1, workers=3)
        self.assertLessEqual(len(sent), 3)

    def test_what_earlier_invocations_spent_counts_toward_the_cap(self):
        def earlier(out):
            (out / "B1.jsonl").write_text(json.dumps({"seq": 0, "task_id": "t0", "step": 0, "run": "B1", "http": 200,
                                                      "answers": answers(), "usage": {"cost": 0.09}}) + "\n", "utf-8")

        sent, written = self.replay([row(i) for i in range(5)], budget=0.1, before=earlier, cost=0.05)
        self.assertEqual(len(sent), 1)       # 0.09 spent: one more request reaches 0.14 and the cap stops the rest
        self.assertEqual(len(written), 2)    # the earlier reply and the new one

    def test_a_budget_that_covers_the_pass_sends_everything(self):
        sent, written = self.replay([row(i) for i in range(4)], budget=10.0, cost=0.5)
        self.assertEqual((len(sent), len(written)), (4, 4))


class ScorerGate(unittest.TestCase):
    def judge(self, **kw):
        return SCORE.judge(row(), {"http": 200, "answers": answers(**kw)})

    def test_a_right_answer_with_its_target_passes_right(self):
        self.assertEqual(self.judge()["gate"], "passed_right")

    def test_a_wrong_but_complete_answer_still_passes_wrong(self):
        result = self.judge(element="e2")
        self.assertEqual((result["ok"], result["gate"]), (False, "passed_wrong"))

    def test_an_action_without_its_required_target_is_an_error_not_a_gate_passage(self):
        result = self.judge(element=None)
        self.assertEqual((result["ok"], result["gate"]), (False, "error"))

    def test_a_target_that_was_not_offered_is_an_error(self):
        self.assertEqual(self.judge(element="zz")["gate"], "error")

    def test_an_action_that_was_not_offered_is_an_error(self):
        self.assertEqual(self.judge(action="switch_tab")["gate"], "error")

    def test_a_wrong_action_without_its_target_is_an_error_too(self):
        result = SCORE.judge(row(), {"http": 200, "answers": {"next_action": {"choice": "scroll", "confidence": 0.99}}})
        self.assertEqual(result["gate"], "error")

    def test_finish_needs_no_target(self):
        a = {"next_action": {"choice": "finish", "confidence": 0.9}, "goal_reached": {"noul": 0.9}}
        self.assertEqual(SCORE.judge(row(), {"http": 200, "answers": a})["gate"], "passed_wrong")


if __name__ == "__main__":
    unittest.main()
