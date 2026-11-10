"""Exploratory, not in PROTOCOL.md: one threshold per model, the lowest that keeps 'passed and wrong' at or below
Jev 1.14's rate at the ReflexMesh thresholds, and how many steps each model then passes rightly. The threshold is
fitted on the same 300 steps it is scored on, so every row is optimistic.

  python equal_risk.py --results results/
"""

import argparse
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from score import judge  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--results", required=True, type=Path)
args = ap.parse_args()
ids = (args.results / "sample-main.txt").read_text("utf-8").split()
models = {}
for p in glob.glob(str(args.results / "*.main.jsonl")):
    rows = {}
    for line in open(p, encoding="utf-8"):
        if line.strip():
            r = json.loads(line)
            rows[r["id"]] = r
    models[next(iter(rows.values()))["model"]] = [judge(rows[i]) if i in rows else None for i in ids]


def at(js, thr):
    right = wrong = 0
    for j in js:
        if not j or not j["valid"] or j["gate"] in ("ungated_right", "ungated_wrong"):
            continue
        if j["weakest"] is not None and j["weakest"] >= thr:
            right += j["step_ok"]
            wrong += not j["step_ok"]
    return right / len(ids), wrong / len(ids)


target = sum(1 for j in models["~typesafe/jev-latest"] if j and j["gate"] == "passed_wrong") / len(ids)
print(f"risk budget (Jev 1.14 passed_wrong at 0.7/0.5): {target:.3f}")
for m, js in sorted(models.items()):
    for t in [x / 100 for x in range(5, 100)]:
        r, w = at(js, t)
        if w <= target + 1e-9:
            print(f"{m:<40} threshold {t:.2f}  passed_right {r:.3f}  passed_wrong {w:.3f}")
            break
