"""Run the frozen deterministic acceptance manifest without dropping pending rows."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path, help="new JSON evidence file")
    options = parser.parse_args()
    manifest_path = Path(__file__).with_name("u-manifest.json")
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema_version") != "v05-u-manifest/0.1":
        raise ValueError("unsupported manifest")
    rows = manifest["cases"]
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)) or any(set(row) != {"id", "expected", "test"} for row in rows):
        raise ValueError("invalid acceptance rows")
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout.strip()
    harness_path = ROOT.parent / "SystemOneHarness"
    harness_commit = (subprocess.run(["git", "rev-parse", "HEAD"], cwd=harness_path,
                                     capture_output=True, text=True, check=True).stdout.strip()
                      if harness_path.is_dir() else None)
    report = {"schema_version": "v05-u-results/0.1", "manifest_sha256": hashlib.sha256(raw).hexdigest(),
              "git_head": git, "python": platform.python_version(), "platform": platform.platform(),
              "harness_commit": harness_commit, "browser_use_version": None,
              "providers": "deterministic fakes and pinned harness script; no live model",
              "command": [sys.executable, str(Path(__file__).resolve().relative_to(ROOT)),
                          "--output", str(options.output)],
              "started_at_unix": time.time(), "cases": [
                  {**row, "status": "pending", "reason": "not_implemented" if row["test"] is None else "not_run"}
                  for row in rows]}
    try:
        report["browser_use_version"] = importlib.metadata.version("browser-use")
    except importlib.metadata.PackageNotFoundError:
        pass
    options.output.parent.mkdir(parents=True, exist_ok=True)
    with options.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)

    for row in report["cases"]:
        if row["test"] is None:
            continue
        suite = unittest.defaultTestLoader.loadTestsFromName(row["test"])
        result = unittest.TestResult()
        started = time.monotonic()
        suite.run(result)
        row["duration_seconds"] = round(time.monotonic() - started, 3)
        if result.testsRun != 1 or result.errors or result.failures:
            row.update(status="failed", reason="test_failure", failures=len(result.errors) + len(result.failures))
        elif result.skipped:
            row.update(status="pending", reason="skipped_dependency")
        else:
            row.update(status="passed", reason="assertions_passed")
        # Rewrite after each case so an interrupted run still shows every planned attempt.
        checkpoint = options.output.with_suffix(options.output.suffix + ".tmp")
        checkpoint.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        checkpoint.replace(options.output)
    counts = {key: sum(row["status"] == key for row in report["cases"])
              for key in ("passed", "pending", "failed")}
    report["counts"] = counts
    report["finished_at_unix"] = time.time()
    options.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(counts))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
