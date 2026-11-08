"""Stage 0 of docs/specs/V0.6-focus-region.md: the same 300 main-sample steps with every target question cut to
K options. The gold operation's question keeps all gold keys and random others up to K; every other target
question keeps K random keys; `state.elements` keeps only the elements some kept key points at (a select key is
`element:option`). Order and numbering stay as on the page. Gold-informed on purpose: this measures the effect of
list length when the right element is surely offered, not a method.

  python stage0_prune.py --data benchmark.jsonl --sample results/sample-main.txt --k 15 --out stage0-k15.jsonl
"""

import argparse
import json
import random
from pathlib import Path

TARGET = {"CLICK": "click_target", "TYPE_TEXT": "type_text_target", "SELECT": "select_target"}


def cut(keys: list[str], keep: set[str], k: int, rng: random.Random) -> list[str]:
    if len(keys) <= k:
        return keys
    others = [x for x in keys if x not in keep]
    chosen = set(keep) | set(rng.sample(others, max(0, k - len(keep))))
    return [x for x in keys if x in chosen]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--sample", required=True, type=Path)
    ap.add_argument("--k", required=True, type=int)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    ids = set(args.sample.read_text("utf-8").split())
    n = cut_q = 0
    with args.data.open(encoding="utf-8") as f, args.out.open("w", encoding="utf-8") as out:
        for line in f:
            rec = json.loads(line)
            rid = rec["meta"]["id"]
            if rid not in ids:
                continue
            n += 1
            gold_q = TARGET.get(rec["gold"]["operation"])
            questions = rec["request"]["questions"]
            kept_elements = set()
            for q, spec in questions.items():
                if q == "operation":
                    continue
                keys = list(spec["criteria"])
                keep = set(rec["gold"].get(q) or []) if q == gold_q else set()
                rng = random.Random(f"stage0:{args.k}:{rid}:{q}")
                new = cut(keys, keep, args.k, rng)
                cut_q += len(new) < len(keys)
                spec["criteria"] = {x: spec["criteria"][x] for x in new}
                kept_elements |= {x.partition(":")[0] for x in new}
            state = rec["request"]["state"]
            state["elements"] = [e for e in state["elements"] if str(e["index"]) in kept_elements]
            rec["meta"]["stage0_k"] = args.k
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"k={args.k}: {n} steps written, {cut_q} target questions cut")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
