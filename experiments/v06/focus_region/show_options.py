"""Print what a recording offered at each step: per target question, key, description and a short container
summary. For writing task scripts from exploration dumps.

  python show_options.py steps/books-explore.explore.jsonl [--max 60]
"""

import argparse
import json
from pathlib import Path


def summary(chain: list[dict]) -> str:
    parts = []
    for e in chain:
        if e.get("boundary") or e.get("tag") in ("body", "html"):
            break
        label = e.get("name") or e.get("aria_label") or e.get("heading") or (e.get("text") or "")[:40]
        if label:
            parts.append(f"{e['tag']}:{label[:40]}" + (f"#{e['pos']}/{e['of']}" if (e.get("of") or 0) > 1 else ""))
        if len(parts) == 2:
            break
    return " < ".join(parts)


ap = argparse.ArgumentParser()
ap.add_argument("path", type=Path)
ap.add_argument("--max", type=int, default=80)
args = ap.parse_args()
for line in args.path.read_text("utf-8").splitlines():
    r = json.loads(line)
    print(f"=== step {r['step']} {r.get('url')}  script={r['script_step']}  gold={r.get('gold')}  {r.get('mismatch') or ''}")
    for q, spec in r["questions"].items():
        if spec.get("type") != "choice" or q in ("next_action", "scroll__direction"):
            continue
        crit = spec.get("criteria") or {}
        print(f"  {q} ({len(crit)})")
        for k, d in list(crit.items())[: args.max]:
            print(f"    {k:>6}  {str(d)[:70]:<70}  {summary(r['containers'].get(k) or [])}")
