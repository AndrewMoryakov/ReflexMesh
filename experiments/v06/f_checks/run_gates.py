"""Run the tests named in ledger.json three times, each pass in a fresh process, and report every F gate item.

Section 16 of V0.6-browser-core: browser checks repeat three times from a fresh state, and an item counts only
when all its tests pass in every pass. Run from the repository root:

  python experiments/v06/f_checks/run_gates.py --out f-gates/ [--passes 3] [--only F02,F05]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
LINE = re.compile(r"^(test\w+) \(([\w.]+)\)(?:\n.*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$", re.M)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--passes", type=int, default=3)
    ap.add_argument("--only")
    args = ap.parse_args()
    ledger = json.loads((HERE / "ledger.json").read_text("utf-8"))
    gates = [g for g in ledger if not args.only or g in args.only.split(",")]
    tests = sorted({t for g in gates for item in ledger[g]["items"] for t in item["tests"]})
    args.out.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run(["git", "-c", "safe.directory=*", "log", "-1", "--format=%H"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    meta = {"commit": commit, "python": platform.python_version(), "platform": platform.platform(),
            "browser": os.environ.get("REFLEXMESH_CHROME"), "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "tests": len(tests)}
    outcomes: dict[str, list[str]] = {t: [] for t in tests}
    for n in range(1, args.passes + 1):
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, "-m", "unittest", "-v", *tests], cwd=ROOT, capture_output=True, text=True,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        (args.out / f"pass{n}.log").write_text(proc.stderr, "utf-8")
        seen = {f"{m.group(2)}": m.group(3) for m in LINE.finditer(proc.stderr)}
        for t in tests:
            outcomes[t].append(seen.get(t, "missing"))
        print(f"pass {n}: {sum(v == 'ok' for v in seen.values())}/{len(tests)} ok in {time.monotonic() - t0:.0f} s", flush=True)
    report = {"meta": meta, "tests": outcomes, "gates": {}}
    for g in gates:
        rows = []
        for item in ledger[g]["items"]:
            if not item["tests"]:
                status = "gap"
            else:
                ok = all(all(o == "ok" for o in outcomes[t]) for t in item["tests"])
                status = ("partial" if "partial" in item else "pass") if ok else "fail"
            rows.append({"need": item["need"], "status": status})
            print(f"{g} {status:<8} {item['need']}")
        report["gates"][g] = rows
    (args.out / "report.json").write_text(json.dumps(report, indent=1), "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
