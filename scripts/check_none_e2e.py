"""ADR-0003 live check: run the V0.3b dataset once through the ReflexMesh CLI (default NONE candidate).

Verifies end to end that the adapter reproduces the V0.3b J1 behaviour. Writes decisions.jsonl and
report.json (V0.3b metrics for comparator "adapter") to a new directory outside the repository.
"""

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v03_run  # noqa: E402
import v03b_score  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--jev-url", default="http://127.0.0.1:8787")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    out = args.output_dir.resolve()
    if out.is_relative_to(ROOT) or out.exists():
        parser.error("output directory must be new and outside the repository")
    out.mkdir(parents=True)
    dataset = v03_run.load_dataset(ROOT / "experiments" / "v03b" / "dataset.jsonl")
    meta = {"reflexmesh_commit": v03_run.git(ROOT, "rev-parse", "HEAD"),
            "reflexmesh_dirty": bool(v03_run.git(ROOT, "status", "--porcelain")),
            "cases": len(dataset), "jev_url": args.jev_url,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    records = []
    for case in dataset:
        task = {"schema_version": "0.1", "task_id": case["case_id"], "goal": case["goal"],
                "capabilities": case["capabilities"], "allowed_routes": case["allowed_routes"]}
        path = out / "task.json"
        path.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        start = time.monotonic()
        done = v03_run.subprocess.run([sys.executable, "-m", "reflexmesh", "route", "--provider", "jevrouter",
                                       "--jev-url", args.jev_url, "--timeout", str(args.timeout),
                                       "--input", str(path)],
                                      cwd=ROOT, env={**v03_run.os.environ, "PYTHONPATH": str(ROOT / "src")},
                                      capture_output=True, timeout=args.timeout + 10, check=False)
        try:
            result = json.loads(done.stdout)
        except ValueError:
            result = {}
        status = {"selected": "selected", "abstained": "abstained",
                  "needs_confirmation": "abstained"}.get(result.get("status"), "error")
        upstream = (result.get("trace") or {}).get("upstream") or {}
        record = {"comparator": "adapter", "run": 1, "case_id": case["case_id"], "status": status,
                  "route": result.get("route") if status == "selected" else None,
                  "reason": result.get("reason_code"), "exit_code": done.returncode,
                  "upstream_selected": upstream.get("selected"), "probabilities": upstream.get("probabilities"),
                  "decision_id": upstream.get("decision_id"),
                  "latency_ms": round((time.monotonic() - start) * 1000, 3)}
        records.append(record)
        print(f"{case['case_id']}: {status} {record['route']} {record['reason']}", flush=True)
    (out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                                         encoding="utf-8")
    metrics = v03b_score.score(records, dataset)["adapter"]
    meta["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (out / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    (out / "report.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: metrics[k] for k in ("control_accuracy", "control_false_abstention", "refusal_abstention",
                                              "refusal_abstention_by_subtype", "forbidden", "error_rate",
                                              "refusal_missed_cases", "abstain_reasons")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
