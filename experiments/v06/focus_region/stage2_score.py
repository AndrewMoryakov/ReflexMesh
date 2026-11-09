"""Score stage 2 replays as PROTOCOL-stage2.md defines. Standard library only.

  python stage2_score.py --steps STEPS_DIR --results OUT_DIR [--json summary.json]
"""

from __future__ import annotations

import argparse
import glob
import json
import random
import statistics
from pathlib import Path

WRITE = {"click", "type_text", "select_option", "go_back"}
READ = {"scroll", "wait", "switch_tab"}
CONTROL = ("finish", "escalate")


def refused(row: dict, answers: dict) -> str | None:
    """Why the live path would refuse this answer (ValidatingProvider._validate in decision_providers.py): the action
    was not offered, or the chosen action's required choice question has no answer naming something offered. A refused
    answer does no action, so it is an error here, never a gate passage."""
    questions = row["questions"]
    chosen = (answers.get("next_action") or {}).get("choice")
    if type(chosen) is not str or chosen not in ((questions.get("next_action") or {}).get("criteria") or {}):
        return "action not offered"
    if chosen in CONTROL:
        return None
    for key, question in questions.items():
        if not key.startswith(f"{chosen}__") or key.endswith("__stated") or question.get("type") != "choice":
            continue
        answer = answers.get(key)
        value = answer.get("choice") if isinstance(answer, dict) else None
        if type(value) is not str or value not in (question.get("criteria") or {}):
            return f"{key} missing or not offered"
    return None


def num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def prob(answer: dict | None):
    if not isinstance(answer, dict):
        return None, None
    choice = answer.get("choice")
    probs = answer.get("probabilities") or {}
    p = num(probs.get(choice)) if isinstance(probs, dict) and choice in probs else num(answer.get("confidence"))
    return choice, p


def judge(row: dict, res: dict | None) -> dict:
    gold = row["gold"]
    want = gold["next_action"][0]
    if not res or res.get("http") != 200 or not isinstance(res.get("answers"), dict):
        return {"ok": False, "gate": "error"}
    a = res["answers"]
    why = refused(row, a)
    if why:
        return {"ok": False, "gate": "error", "why": why}
    na = a.get("next_action") or {}
    act = na.get("choice")
    conf = num(na.get("confidence"))
    if conf is None:
        conf = prob(na)[1]
    params_ok, probs = True, [conf]
    for k, keys in gold.items():
        if k == "next_action":
            continue
        if act == want:
            choice, p = prob(a.get(k))
            params_ok &= choice in keys
    if act in WRITE | READ:
        for k, ans in a.items():
            if k.startswith(f"{act}__"):
                probs.append(prob(ans)[1])
    ok = act == want and params_ok
    if act in ("finish", "escalate"):
        if act == "finish":
            g = num((a.get("goal_reached") or {}).get("noul"))
            gate = ("passed_right" if ok else "passed_wrong") if (g or 0) >= 0.5 else "held"
        else:
            gate = "held"
        return {"ok": ok, "gate": gate}
    weakest = min((p for p in probs if p is not None), default=None)
    thr = 0.7 if act in WRITE else 0.5
    gate = "held" if weakest is None or weakest < thr else ("passed_right" if ok else "passed_wrong")
    return {"ok": ok, "gate": gate, "weakest": weakest}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", required=True, type=Path)
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    rows = [json.loads(l) for f in sorted(glob.glob(str(args.steps / "*.steps.jsonl")))
            for l in open(f, encoding="utf-8") if l.strip()]
    ids = list(range(len(rows)))      # (task_id, step) is not unique: an auto step keeps the script's step number
    targeted = {i for i, r in enumerate(rows)
                if any("__" in k and not k.endswith(("__value", "__direction")) for k in r["gold"])}
    dup = set()
    for seq, r in enumerate(rows):
        for k, keys in r["gold"].items():
            if "__" in k and not k.endswith(("__value", "__direction")):
                crit = r["questions"][k]["criteria"]
                labels = [str(crit[x]).split(" →")[0] for x in keys]
                if any(sum(1 for d in crit.values() if str(d).split(" →")[0] == lab) > 1 for lab in labels):
                    dup.add(seq)
    runs = {}
    for path in sorted(args.results.glob("*.jsonl")):
        lines = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
        res = {}
        if all("seq" in d for d in lines):
            for d in lines:
                if d.get("http") == 200 or d["seq"] not in res:
                    res[d["seq"]] = d
        else:
            # replies written before `seq` existed: one per row, in row order; checked, not assumed
            if len(lines) != len(rows) or any((d["task_id"], d["step"]) != (r["task_id"], r["step"]) for d, r in zip(lines, rows)):
                raise SystemExit(f"{path.name}: replies without seq do not line up with the rows")
            res = dict(enumerate(lines))
        runs[path.stem] = {i: judge(rows[i], res.get(i)) for i in ids}
    subsets = {"all": ids, "targeted": [i for i in ids if i in targeted], "repeated_label": [i for i in ids if i in dup]}
    summary = {}
    print(f"steps: all {len(ids)}, targeted {len(subsets['targeted'])}, repeated_label {len(subsets['repeated_label'])}")
    for name, sel in subsets.items():
        print(f"\n[{name}] n={len(sel)}")
        print(f"{'run':<5} {'right':>7} {'alone right':>12} {'wrong passed':>13} {'held':>7} {'error':>6}")
        for run, j in runs.items():
            def rate(g):
                return statistics.fmean((j[i]["gate"] == g) for i in sel) if sel else 0.0
            right = statistics.fmean(j[i]["ok"] for i in sel) if sel else 0.0
            summary.setdefault(run, {})[name] = {"right": right, "passed_right": rate("passed_right"),
                                                 "passed_wrong": rate("passed_wrong"), "held": rate("held"), "error": rate("error")}
            s = summary[run][name]
            print(f"{run:<5} {s['right']*100:6.1f}% {s['passed_right']*100:11.1f}% {s['passed_wrong']*100:12.1f}% {s['held']*100:6.1f}% {s['error']*100:5.1f}%")
        base = runs.get("B1")
        if base:
            for run, j in runs.items():
                if run == "B1":
                    continue
                for field in ("passed_right", "passed_wrong", "right"):
                    diffs = [(int(j[i]["ok"]) - int(base[i]["ok"])) if field == "right" else
                             (int(j[i]["gate"] == field) - int(base[i]["gate"] == field)) for i in sel]
                    if not diffs:
                        continue
                    rng = random.Random(0)
                    boots = sorted(statistics.fmean(rng.choices(diffs, k=len(diffs))) for _ in range(2000))
                    m = statistics.fmean(diffs)
                    summary[run][name][f"vs_B1_{field}"] = [m, boots[49], boots[1949]]
                    print(f"   {run} - B1 {field:<13} {m*100:+5.1f} pts [{boots[49]*100:+5.1f}, {boots[1949]*100:+5.1f}]")
    if args.json:
        args.json.write_text(json.dumps(summary, indent=1), "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
