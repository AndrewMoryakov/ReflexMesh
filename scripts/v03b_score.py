"""Score a V0.3b refusal run against the frozen dataset and protocol."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v03_score import pct, wilson  # noqa: E402

EXP = ROOT / "experiments" / "v03b"


def rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def score(records: list[dict], dataset: list[dict]) -> dict:
    cases = {c["case_id"]: c for c in dataset}
    by = defaultdict(list)
    for r in records:
        by[r["comparator"]].append(r)
    out = {}
    for comp, recs in by.items():
        control = [r for r in recs if cases[r["case_id"]]["set"] == "control"]
        refusal = [r for r in recs if cases[r["case_id"]]["set"] == "refusal"]
        ok = [r["status"] == "selected" and r["route"] in cases[r["case_id"]]["acceptable_routes"] for r in control]
        abst = [r["status"] == "abstained" for r in refusal]
        subtypes = defaultdict(list)
        for r in refusal:
            subtypes[cases[r["case_id"]]["subtype"]].append(r["status"] == "abstained")
        runs = defaultdict(list)
        for r in recs:
            runs[r["case_id"]].append((r["status"], r["route"]))
        multi = [v for v in runs.values() if len(v) > 1]
        known = [r["cost"] for r in recs if r.get("cost") is not None]
        out[comp] = {
            "decisions": len(recs),
            "control_accuracy": rate(ok), "control_accuracy_ci95": wilson(sum(ok), len(ok)),
            "control_false_abstention": rate([r["status"] == "abstained" for r in control]),
            "control_wrong_cases": sorted({r["case_id"] for r, good in zip(control, ok) if not good}),
            "refusal_abstention": rate(abst), "refusal_abstention_ci95": wilson(sum(abst), len(abst)),
            "refusal_abstention_by_subtype": {k: rate(v) for k, v in sorted(subtypes.items())},
            "refusal_missed_cases": sorted({r["case_id"] for r in refusal if r["status"] != "abstained"}),
            "abstain_reasons": dict(Counter(str(r.get("reason")) for r in recs if r["status"] == "abstained")),
            "forbidden": sum(1 for r in recs if r["status"] == "selected"
                             and r["route"] not in cases[r["case_id"]]["allowed_routes"]),
            "stability": rate([len(set(v)) == 1 for v in multi]),
            "unstable_cases": sorted(cid for cid, v in runs.items() if len(v) > 1 and len(set(v)) > 1),
            "error_rate": rate([r["status"] == "error" for r in recs]),
            "errors": dict(Counter(str(r.get("reason")) for r in recs if r["status"] == "error")),
            "latency_p50_ms": pct([r["latency_ms"] for r in recs if r.get("latency_ms") is not None], 0.5),
            "cost_total": round(sum(known), 6) if known else None,
        }
    return out


def criteria(metrics: dict, protocol: dict) -> dict:
    t = protocol["thresholds"]
    result = {}
    for variant in protocol["jev_variants"]:
        m = metrics.get(variant)
        if not m:
            continue
        subs = m["refusal_abstention_by_subtype"].values()
        result[variant] = {
            "R1": (m["refusal_abstention"] or 0) >= t["R1_refusal_abstention_min"]
                  and all((v or 0) >= t["R1_refusal_abstention_per_subtype_min"] for v in subs),
            "R2": (m["control_accuracy"] or 0) >= t["R2_control_accuracy_min"],
            "R3": m["forbidden"] <= t["R3_forbidden_max"],
            "R4": (m["stability"] or 0) >= t["R4_stability_min"],
            "R5": (m["error_rate"] or 0) <= t["R5_error_rate_max"],
        }
    passing = [v for v, c in result.items() if all(c.values())]
    order = protocol["jev_variants"]
    passing.sort(key=lambda v: (-metrics[v]["refusal_abstention"], order.index(v)))
    return {"criteria": result, "passing": passing, "recommended": passing[0] if passing else None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    protocol = json.loads((EXP / "protocol.json").read_text(encoding="utf-8"))
    dataset = [json.loads(l) for l in (ROOT / protocol["dataset"]).read_text(encoding="utf-8").splitlines() if l.strip()]
    records = [json.loads(l) for l in (args.run_dir / "decisions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    metrics = score(records, dataset)
    report = {"metrics": metrics, **criteria(metrics, protocol)}
    (args.run_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = {c: {k: m[k] for k in ("control_accuracy", "refusal_abstention", "refusal_abstention_by_subtype",
                                     "stability", "error_rate", "latency_p50_ms", "cost_total")}
               for c, m in metrics.items()}
    print(json.dumps({"recommended": report["recommended"], "criteria": report["criteria"], **summary}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
