"""Score the JSONL results of run_openrouter.py as PROTOCOL.md defines. Standard library only.

  python score.py --results results/ --sample main [--json summary.json]
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

TARGET_OPS = {"CLICK", "TYPE_TEXT", "SELECT"}
WRITE_OPS = {"CLICK", "TYPE_TEXT", "SELECT"}
UNGATED = {"DONE", "BLOCKED"}
BASELINE = "~typesafe/jev-latest"


def num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def judge(r: dict) -> dict:
    """One step: correctness, the gate's reading, its outcome."""
    gold_op = r.get("gold_operation")
    op = r.get("operation") or {}
    choice = op.get("choice")
    valid = r.get("http") == 200 and choice is not None
    op_ok = valid and choice == gold_op
    gq = r.get("gold_question") or {}
    ele_ok = None
    if gold_op in TARGET_OPS:
        ele_ok = valid and gq.get("choice") in (r.get("gold_target") or [])
    step_ok = bool(op_ok and (ele_ok if gold_op in TARGET_OPS else True))
    c_op = num(op.get("confidence"))
    if c_op is None:
        c_op = num(op.get("p_choice"))
    weakest = c_op
    if valid and choice in TARGET_OPS:
        t = r.get("chosen_target") or {}
        p_t = num(t.get("p_choice"))
        if p_t is None:
            p_t = num(t.get("confidence"))
        weakest = None if (p_t is None or c_op is None) else min(c_op, p_t)
    if not valid:
        gate = "error"
    elif choice in UNGATED:
        gate = "ungated_wrong" if not step_ok else "ungated_right"
    else:
        thr = 0.7 if choice in WRITE_OPS else 0.5
        if weakest is None or weakest < thr:
            gate = "held"
        else:
            gate = "passed_right" if step_ok else "passed_wrong"
    return {"valid": valid, "op_ok": op_ok, "ele_ok": ele_ok, "step_ok": step_ok, "c_op": c_op,
            "weakest": weakest, "gate": gate}


def ece(pairs: list[tuple[float, bool]], bins: int = 10) -> float | None:
    pairs = [(p, y) for p, y in pairs if p is not None]
    if not pairs:
        return None
    total, err = len(pairs), 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        inside = [(p, y) for p, y in pairs if (lo <= p < hi) or (b == bins - 1 and p == 1.0)]
        if inside:
            err += len(inside) / total * abs(statistics.fmean(p for p, _ in inside) - statistics.fmean(1.0 if y else 0.0 for _, y in inside))
    return err


def coverage_at(pairs: list[tuple[float | None, bool]], precision: float, n_all: int) -> float:
    """Largest share of all steps that can be accepted, highest confidence first, keeping precision."""
    ranked = sorted(((p, y) for p, y in pairs if p is not None), key=lambda t: -t[0])
    best, right = 0, 0
    for i, (_, y) in enumerate(ranked, 1):
        right += y
        if right / i >= precision:
            best = i
    return best / n_all if n_all else 0.0


def pct(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None, None
    return xs[len(xs) // 2], xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--sample", choices=("pilot", "main"), required=True)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    ids = [l for l in (args.results / f"sample-{args.sample}.txt").read_text("utf-8").split() if l]
    n_all = len(ids)
    per_model: dict[str, dict[str, dict]] = {}
    for path in sorted(args.results.glob(f"*.{args.sample}.jsonl")):
        rows = {}
        for line in path.read_text("utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[r["id"]] = r
        model = next(iter(rows.values()))["model"] if rows else path.stem
        per_model[model] = rows

    summary = {}
    judged_all = {}
    for model, rows in per_model.items():
        judged = {i: judge(rows[i]) if i in rows else {"valid": False, "op_ok": False, "ele_ok": None,
                  "step_ok": False, "c_op": None, "weakest": None, "gate": "not_sent"} for i in ids}
        judged_all[model] = judged
        vals = list(judged.values())
        gates = Counter(v["gate"] for v in vals)
        targeted = [v for i, v in judged.items() if (rows.get(i) or {}).get("gold_operation") in TARGET_OPS]
        lat = [r.get("latency_ms") for r in rows.values() if r.get("http") == 200]
        tokens = [num((r.get("usage") or {}).get("input_tokens") or (r.get("usage") or {}).get("prompt_tokens")) for r in rows.values()]
        errors = Counter(str(r.get("http")) for r in rows.values() if r.get("http") != 200 or not r.get("operation"))
        by_source = defaultdict(list)
        for i, v in judged.items():
            by_source[(rows.get(i) or {}).get("source") or "?"].append(v["step_ok"])
        p50, p95 = pct(lat)
        summary[model] = {
            "sent": len(rows), "valid": sum(v["valid"] for v in vals), "errors": dict(errors),
            "step_success": sum(v["step_ok"] for v in vals) / n_all,
            "op_accuracy": sum(v["op_ok"] for v in vals) / n_all,
            "element_accuracy": (sum(bool(v["ele_ok"]) for v in targeted) / len(targeted)) if targeted else None,
            "passed_right": gates["passed_right"] / n_all, "passed_wrong": gates["passed_wrong"] / n_all,
            "held": gates["held"] / n_all, "ungated_wrong": gates["ungated_wrong"] / n_all,
            "coverage_at_95": coverage_at([(v["weakest"], v["step_ok"]) for v in vals], 0.95, n_all),
            "ece_operation": ece([(v["c_op"], v["op_ok"]) for v in vals if v["valid"]]),
            "latency_ms_p50": p50, "latency_ms_p95": p95,
            "input_tokens_mean": statistics.fmean(t for t in tokens if t) if any(tokens) else None,
            "cost_usd": sum(float(r.get("cost_usd") or 0) for r in rows.values()),
            "step_success_by_source": {s: sum(x) / len(x) for s, x in sorted(by_source.items())},
            "served_models": sorted({str(r.get("served_model")) for r in rows.values() if r.get("served_model")}),
        }

    if BASELINE in judged_all:
        base = judged_all[BASELINE]
        rng = random.Random(0)
        for model, judged in judged_all.items():
            if model == BASELINE:
                continue
            diffs = [int(judged[i]["step_ok"]) - int(base[i]["step_ok"]) for i in ids]
            boots = sorted(statistics.fmean(rng.choices(diffs, k=len(diffs))) for _ in range(2000))
            summary[model]["vs_jev_latest"] = {"diff": statistics.fmean(diffs), "ci95": [boots[49], boots[1949]],
                                               "only_model": diffs.count(1), "only_jev": diffs.count(-1)}

    cols = ("step_success", "passed_right", "passed_wrong", "held", "coverage_at_95", "ece_operation")
    print(f"sample {args.sample}: {n_all} steps")
    print(f"{'model':<44} {'valid':>5} " + " ".join(f"{c[:12]:>12}" for c in cols) + f" {'p50 ms':>7} {'tokens':>7} {'cost $':>7}  vs jev-latest")
    for model, s in sorted(summary.items(), key=lambda kv: -kv[1]["step_success"]):
        vs = s.get("vs_jev_latest")
        vs_txt = f"{vs['diff']:+.3f} [{vs['ci95'][0]:+.3f}, {vs['ci95'][1]:+.3f}]" if vs else ""
        print(f"{model:<44} {s['valid']:>5} " + " ".join(f"{(s[c] if s[c] is not None else float('nan')):>12.3f}" for c in cols)
              + f" {s['latency_ms_p50'] or 0:>7} {round(s['input_tokens_mean'] or 0):>7} {s['cost_usd']:>7.4f}  {vs_txt}")
    if args.json:
        args.json.write_text(json.dumps({"sample": args.sample, "steps": n_all, "models": summary}, indent=1), "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
