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
REQUIRED_PASSES = 3     # section 16: browser checks repeat three times from a fresh state
# The outcome of each test is taken from unittest's result object in the child, not parsed from its text output:
# the driver logs into the same stream and splits the "... ok" lines.
CHILD = r"""
import json, sys, unittest
out, names = sys.argv[1], sys.argv[2:]
class Result(unittest.TextTestResult):
    outcomes = {}
    def addSuccess(self, test): super().addSuccess(test); self.outcomes[test.id()] = "ok"
    def addFailure(self, test, err): super().addFailure(test, err); self.outcomes[test.id()] = "FAIL"
    def addError(self, test, err): super().addError(test, err); self.outcomes[test.id()] = "ERROR"
    def addSkip(self, test, reason): super().addSkip(test, reason); self.outcomes[test.id()] = "skipped: " + reason
    def addSubTest(self, test, sub, err):
        super().addSubTest(test, sub, err)
        if err is not None: self.outcomes[test.id()] = "FAIL"
suite = unittest.defaultTestLoader.loadTestsFromNames(names)
unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
json.dump(Result.outcomes, open(out, "w"), indent=1)
"""
LINE = re.compile(r"^(test\w+) \(([\w.]+)\)(?:\n.*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$", re.M)


def judge(item: dict, outcomes: dict[str, list[str]], passes: int) -> str:
    """One ledger item: `gap` without tests, `fail` unless every test was ok in every one of `passes` passes,
    `diagnostic` when it did but fewer than the required passes ran (not acceptance), else `partial` or `pass`."""
    if not item["tests"]:
        return "gap"
    if passes < 1 or not all(len(outcomes[t]) == passes and all(o == "ok" for o in outcomes[t]) for t in item["tests"]):
        return "fail"
    if passes < REQUIRED_PASSES:
        return "diagnostic"
    return "partial" if "partial" in item else "pass"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--passes", type=int, default=REQUIRED_PASSES)
    ap.add_argument("--only")
    args = ap.parse_args(argv)
    if args.passes < 1:
        ap.error("--passes must be at least 1 (and at least %d for a result that counts as acceptance)" % REQUIRED_PASSES)
    ledger = json.loads((HERE / "ledger.json").read_text("utf-8"))
    gates = [g for g in ledger if not args.only or g in args.only.split(",")]
    tests = sorted({t for g in gates for item in ledger[g]["items"] for t in item["tests"]})
    args.out.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run(["git", "-c", "safe.directory=*", "log", "-1", "--format=%H"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    meta = {"commit": commit, "python": platform.python_version(), "platform": platform.platform(),
            "browser": os.environ.get("REFLEXMESH_CHROME"), "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "tests": len(tests),
            "passes": args.passes, "required_passes": REQUIRED_PASSES}
    outcomes: dict[str, list[str]] = {t: [] for t in tests}
    returncodes, crashed = [], 0
    for n in range(1, args.passes + 1):
        t0 = time.monotonic()
        result_file = args.out / f"pass{n}.json"
        result_file.unlink(missing_ok=True)       # an earlier run's results must never stand in for this pass
        proc = subprocess.run([sys.executable, "-c", CHILD, str(result_file), *tests], cwd=ROOT, capture_output=True,
                              text=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        returncodes.append(proc.returncode)
        (args.out / f"pass{n}.log").write_text(proc.stdout + proc.stderr, "utf-8")
        # A pass counts only when its child ended cleanly and wrote its results; otherwise every test is `missing`.
        seen = json.loads(result_file.read_text("utf-8")) if proc.returncode == 0 and result_file.exists() else {}
        if proc.returncode != 0 or not result_file.exists():
            crashed += 1
            print(f"pass {n}: the child ended with code {proc.returncode} and "
                  f"{'wrote' if result_file.exists() else 'wrote no'} results; the pass counts for nothing", flush=True)
        for t in tests:
            outcomes[t].append(seen.get(t, "missing"))
        print(f"pass {n}: {sum(v == 'ok' for v in seen.values())}/{len(tests)} ok in {time.monotonic() - t0:.0f} s", flush=True)
    meta["child_returncodes"] = returncodes
    report = {"meta": meta, "tests": outcomes, "gates": {}}
    for g in gates:
        rows = []
        for item in ledger[g]["items"]:
            status = judge(item, outcomes, args.passes)
            rows.append({"need": item["need"], "status": status})
            print(f"{g} {status:<8} {item['need']}")
        report["gates"][g] = rows
    (args.out / "report.json").write_text(json.dumps(report, indent=1), "utf-8")
    if args.passes < REQUIRED_PASSES:
        print(f"{args.passes} pass(es) of the {REQUIRED_PASSES} section 16 requires: items that passed are `diagnostic`, "
              "not acceptance", flush=True)
    return 2 if crashed else 0


if __name__ == "__main__":
    raise SystemExit(main())
