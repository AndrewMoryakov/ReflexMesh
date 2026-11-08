"""Stage 0: Jev 1.14 on the cut lists against Jev 1.14 on the full lists, paired over the same steps.

  python stage0_compare.py --full results/ --cut results-k15/ results-k40/
"""

import argparse
import json
import os
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from score import judge  # noqa: E402

FILE = "latest-typesafe__jev-latest.main.jsonl"


def load(d: Path) -> dict:
    rows = {}
    for line in (d / FILE).read_text("utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            rows[r["id"]] = judge(r)
    return rows


def paired(ids, a, b, field):
    diffs = [int(b[i]["gate"] == field if field != "step_ok" else b[i]["step_ok"])
             - int(a[i]["gate"] == field if field != "step_ok" else a[i]["step_ok"]) for i in ids]
    rng = random.Random(0)
    boots = sorted(statistics.fmean(rng.choices(diffs, k=len(diffs))) for _ in range(2000))
    return statistics.fmean(diffs), boots[49], boots[1949]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", required=True, type=Path)
    ap.add_argument("--cut", required=True, nargs="+", type=Path)
    args = ap.parse_args()
    ids = (args.full / "sample-main.txt").read_text("utf-8").split()
    full = load(args.full)
    fields = ("step_ok", "passed_right", "passed_wrong", "held", "ungated_wrong")
    def rates(rows):
        return {f: statistics.fmean((rows[i]["step_ok"] if f == "step_ok" else rows[i]["gate"] == f) for i in ids) for f in fields}
    print(f"{'list':<10} " + " ".join(f"{f:>14}" for f in fields))
    print(f"{'full':<10} " + " ".join(f"{v * 100:>13.1f}%" for v in rates(full).values()))
    for d in args.cut:
        cut = load(d)
        missing = [i for i in ids if i not in cut]
        if missing:
            print(f"{d}: {len(missing)} steps missing")
            continue
        print(f"{d.name:<10} " + " ".join(f"{v * 100:>13.1f}%" for v in rates(cut).values()))
        for f in fields:
            m, lo, hi = paired(ids, full, cut, f)
            print(f"   {f:<14} {m * 100:+5.1f} pts  [{lo * 100:+5.1f}, {hi * 100:+5.1f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
