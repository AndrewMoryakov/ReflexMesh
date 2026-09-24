"""Score a V0.3 run against the frozen dataset and protocol thresholds."""

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments" / "v03"


def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 4), round(c + h, 4)]


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))], 1)


def correct(rec: dict, case: dict) -> bool:
    return rec["status"] == "selected" and rec["route"] in case["acceptable_routes"]


def score(records: list[dict], dataset: list[dict], protocol: dict) -> dict:
    cases = {c["case_id"]: c for c in dataset}
    primary = set(protocol["primary_kinds"])
    by_comp = defaultdict(list)
    for rec in records:
        by_comp[rec["comparator"]].append(rec)
    out = {}
    for comp, recs in by_comp.items():
        prim = [r for r in recs if cases[r["case_id"]]["kind"] in primary]
        k = sum(correct(r, cases[r["case_id"]]) for r in prim)
        runs = defaultdict(list)
        for r in recs:
            runs[r["case_id"]].append(r)
        majority_k = 0
        for cid, rs in runs.items():
            if cases[cid]["kind"] in primary:
                majority_k += sum(correct(r, cases[cid]) for r in rs) * 2 > len(rs)
        n_prim_cases = sum(1 for cid in runs if cases[cid]["kind"] in primary)
        breakdown = {}
        for key in ("kind", "lang"):
            groups = defaultdict(list)
            for r in prim:
                groups[cases[r["case_id"]][key]].append(correct(r, cases[r["case_id"]]))
            breakdown[key] = {g: round(sum(v) / len(v), 4) for g, v in sorted(groups.items())}
        per_class = defaultdict(list)
        for r in prim:
            case = cases[r["case_id"]]
            if case["kind"] == "single":
                per_class[case["acceptable_routes"][0]].append(correct(r, case))
        refusal = [r for r in recs if cases[r["case_id"]]["kind"] == "refusal"]
        composite = [r for r in recs if cases[r["case_id"]]["kind"] == "composite"]
        multi = [rs for rs in runs.values() if len(rs) > 1]
        stable = sum(len({(r["status"], r["route"]) for r in rs}) == 1 for rs in multi)
        forbidden = [r for r in recs if r["status"] == "selected"
                     and r["route"] not in cases[r["case_id"]]["allowed_routes"]]
        latencies = [r["latency_ms"] for r in recs if r.get("latency_ms") is not None]
        costs = [r.get("cost") for r in recs]
        known = [c for c in costs if c is not None]
        errors = [r for r in recs if r["status"] == "error"]
        confusion = defaultdict(Counter)
        for r in prim:
            confusion["/".join(cases[r["case_id"]]["acceptable_routes"])][r["route"] or r["status"]] += 1
        out[comp] = {
            "decisions": len(recs), "runs": max(r["run"] for r in recs),
            "accuracy": round(k / len(prim), 4) if prim else None, "accuracy_ci95": wilson(k, len(prim)),
            "correct": k, "primary_decisions": len(prim),
            "accuracy_majority": round(majority_k / n_prim_cases, 4) if n_prim_cases else None,
            "accuracy_by": breakdown,
            "accuracy_single_by_class": {c: round(sum(v) / len(v), 4) for c, v in sorted(per_class.items())},
            "coverage": round(sum(r["status"] == "selected" for r in prim) / len(prim), 4) if prim else None,
            "abstention": round(sum(r["status"] == "abstained" for r in prim) / len(prim), 4) if prim else None,
            "error_rate": round(len(errors) / len(recs), 4),
            "errors": Counter(str(r.get("reason")) for r in errors),
            "forbidden": len(forbidden), "forbidden_cases": sorted({r["case_id"] for r in forbidden}),
            "refusal_abstention": (round(sum(r["status"] == "abstained" for r in refusal) / len(refusal), 4)
                                   if refusal else None),
            "refusal_outcomes": dict(Counter(r["route"] or r["status"] for r in refusal)),
            "composite_outcomes": dict(Counter(r["route"] or r["status"] for r in composite)),
            "stability": round(stable / len(multi), 4) if multi else None,
            "unstable_cases": sorted(cid for cid, rs in runs.items()
                                     if len(rs) > 1 and len({(r["status"], r["route"]) for r in rs}) > 1),
            "latency_p50_ms": pct(latencies, 0.5), "latency_p95_ms": pct(latencies, 0.95),
            "calls_total": sum(r.get("calls") or 0 for r in recs),
            "cost_total": round(sum(known), 6) if known else None,
            "cost_known_decisions": len(known),
            "cost_per_decision": round(sum(known) / len(known), 8) if known else None,
            "confusion_primary": {k: dict(v) for k, v in sorted(confusion.items())},
            "wrong_primary": sorted({r["case_id"] for r in prim if not correct(r, cases[r["case_id"]])}),
        }
    return out


def criteria(metrics: dict, protocol: dict) -> dict:
    t = protocol["thresholds"]
    jev, rules_, llm = metrics.get("jev"), metrics.get("rules"), metrics.get("llm")
    res = {}
    if jev:
        res["T1"] = jev["accuracy"] >= t["T1_jev_accuracy_min"]
        res["T2"] = rules_ is not None and jev["accuracy"] - rules_["accuracy"] >= t["T2_jev_minus_rules_min"] - 1e-9
        res["T3"] = llm is not None and jev["accuracy"] - llm["accuracy"] >= t["T3_jev_minus_llm_min"] - 1e-9
        res["T4"] = jev["forbidden"] <= t["T4_jev_forbidden_max"]
        res["T5"] = jev["stability"] is not None and jev["stability"] >= t["T5_jev_stability_min"]
        res["T6"] = jev["error_rate"] <= t["T6_jev_error_rate_max"]
    if res and all(res[k] for k in protocol["decision"]["useful"]):
        verdict = "useful"
    elif res and all(res[k] for k in protocol["decision"]["acceptable"]):
        verdict = "acceptable_no_advantage"
    else:
        verdict = "not_confirmed"
    return {"criteria": res, "verdict": verdict}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    protocol = json.loads((EXP / "protocol.json").read_text(encoding="utf-8"))
    dataset = [json.loads(l) for l in (ROOT / protocol["dataset"]).read_text(encoding="utf-8").splitlines() if l.strip()]
    records = [json.loads(l) for l in (args.run_dir / "decisions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    metrics = score(records, dataset, protocol)
    report = {"metrics": metrics, **criteria(metrics, protocol)}
    (args.run_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "criteria": report["criteria"],
                      **{c: {k: m[k] for k in ("accuracy", "accuracy_ci95", "stability", "forbidden",
                                               "error_rate", "latency_p50_ms", "cost_total")}
                         for c, m in metrics.items()}}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
