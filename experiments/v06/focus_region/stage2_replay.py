"""Stage 2 of docs/specs/V0.6-focus-region.md (see PROTOCOL-stage2.md): replay the frozen stage 1 steps to Jev
through OpenRouter, as recorded (variant B) and with mechanism A (variant A: each offered target's description
followed by its container, built from the recorded raw chain). Standard library only; the OpenRouter key is read
from the first line of stdin and never printed or written. Resumable: answered (variant, pass, step) are skipped.

  python stage2_replay.py --steps STEPS_DIR --out OUT_DIR --runs B1,A1,B2 --budget 0.08 < keyfile
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import copy
import glob
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "~typesafe/jev-latest"
LABEL_CHARS = 60
FORMS = {"form"}
ITEM_TAGS = {"li", "tr", "article"}
ITEM_ROLES = {"listitem", "row", "article"}
LANDMARK_TAGS = {"nav": "navigation", "main": "main", "aside": "aside", "header": "header", "footer": "footer"}
LANDMARK_ROLES = {"navigation", "main", "complementary", "banner", "contentinfo", "search", "region"}


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def container_label(chain: list[dict], repeated: bool = False) -> str | None:
    """Mechanism A, section 3 of the spec: the nearest suitable ancestor, in this order of kinds as met going up:
    a named form; a list item, table row or card (its heading, else its name, else its first words, with its
    position among same-tag siblings); a landmark; else the nearest ancestor's heading. Names and headings only,
    never field values. At most LABEL_CHARS characters.

    `repeated` (variant A2, declared with A before any call): an ancestor that is one of three or more same-tag
    siblings also counts as an item, since cards are often plain `div`s."""
    item_pos = None
    for i, e in enumerate(chain):
        if e.get("boundary") or e.get("tag") in ("body", "html"):
            break
        tag, role = e.get("tag") or "", (e.get("role") or "").lower()
        named = e.get("name") or e.get("aria_label") or e.get("heading")
        if tag in FORMS or role == "form":
            if named:
                return _clip(f'form "{_clip(named, 40)}"', LABEL_CHARS)
            continue
        if tag in ITEM_TAGS or role in ITEM_ROLES or (repeated and (e.get("of") or 0) >= 3):
            label = e.get("heading") or e.get("name") or " ".join(str(e.get("text") or "").split()[:6])
            pos = (e.get("pos"), e.get("of"))
            if (pos[1] or 0) <= 1 and i + 1 < len(chain):       # a card inside a one-card list item
                nxt = chain[i + 1]
                if nxt.get("tag") in ITEM_TAGS and (nxt.get("of") or 0) > 1:
                    pos = (nxt.get("pos"), nxt.get("of"))
            if label:
                where = f", {pos[0]} of {pos[1]}" if (pos[1] or 0) > 1 and pos[0] else ""
                return _clip(f'"{_clip(label, 40)}"{where}', LABEL_CHARS)
            continue
        if tag in LANDMARK_TAGS or role in LANDMARK_ROLES:
            kind = LANDMARK_TAGS.get(tag) or role
            return _clip(f'{kind} "{_clip(named, 40)}"' if named else kind, LABEL_CHARS)
    for e in chain:
        if e.get("boundary") or e.get("tag") in ("body", "html"):
            break
        if e.get("heading"):
            return _clip(f'under "{_clip(e["heading"], 40)}"', LABEL_CHARS)
    return None


def variant_a(row: dict, repeated: bool = False) -> dict:
    questions = copy.deepcopy(row["questions"])
    for q in questions.values():
        if q.get("type") != "choice":
            continue
        crit = q.get("criteria") or {}
        for key, desc in list(crit.items()):
            chain = row["containers"].get(key)
            label = container_label(chain, repeated) if chain else None
            if label and isinstance(desc, str):
                crit[key] = f"{desc} (in {label})"
    return questions


def post(key: str, body: dict) -> tuple[int, dict | str, int]:
    data = json.dumps(body).encode("utf-8")
    delay, last = 2.0, (0, "not sent", 0)
    for _ in range(3):
        req = urllib.request.Request(ENDPOINT, data=data, method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/AndrewMoryakov/ReflexMesh", "X-Title": "ReflexMesh focus-region stage 2"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, json.loads(r.read().decode("utf-8", "replace")), int((time.monotonic() - t0) * 1000)
        except urllib.error.HTTPError as e:
            last = (e.code, e.read().decode("utf-8", "replace")[:500], int((time.monotonic() - t0) * 1000))
            if e.code != 429 and e.code < 500:
                return last
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = (0, f"{type(e).__name__}: {e}"[:300], int((time.monotonic() - t0) * 1000))
        time.sleep(delay)
        delay *= 2
    return last


def recorded_cost(out: Path) -> float:
    """What the replies already written under `out` cost, whichever run wrote them."""
    total = 0.0
    for path in out.glob("*.jsonl"):
        for line in path.read_text("utf-8").splitlines():
            if line.strip():
                total += float(((json.loads(line).get("usage") or {}).get("cost")) or 0.0)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--runs", default="B1,A1,B2")
    ap.add_argument("--budget", type=float, required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry", action="store_true", help="write variant A labels for inspection, call nothing")
    args = ap.parse_args()
    rows = [json.loads(l) for f in sorted(glob.glob(str(args.steps / "*.steps.jsonl")))
            for l in open(f, encoding="utf-8") if l.strip()]
    for seq, r in enumerate(rows):
        r["seq"] = seq             # (task_id, step) is not unique: an auto step keeps the script's step number
    args.out.mkdir(parents=True, exist_ok=True)
    if args.dry:
        with (args.out / "variant-a-labels.txt").open("w", encoding="utf-8") as f:
            for r in rows:
                for k, keys in r["gold"].items():
                    if "__" in k and not k.endswith(("__value", "__direction")):
                        for g in keys:
                            f.write(f'{r["task_id"]} {r["step"]} {k}: A  {variant_a(r)[k]["criteria"].get(g)}\n')
                            f.write(f'{r["task_id"]} {r["step"]} {k}: A2 {variant_a(r, True)[k]["criteria"].get(g)}\n')
        print("dry run written")
        return 0
    key = sys.stdin.readline().strip().lstrip("﻿")
    # The cap is for the stage in all, so what earlier invocations already recorded counts toward it.
    state = {"spent": recorded_cost(args.out), "skipped": 0}
    lock = threading.Lock()
    for run in [x.strip() for x in args.runs.split(",") if x.strip()]:
        path = args.out / f"{run}.jsonl"
        done = set()
        if path.exists():
            done = {d["seq"] for d in map(json.loads, path.read_text("utf-8").splitlines()) if d.get("http") == 200}
        todo = [r for r in rows if r["seq"] not in done]

        def one(r):
            with lock:               # checked before each request, not after the pass: the cap stops the sending
                if state["spent"] >= args.budget:
                    state["skipped"] += 1
                    return None
            questions = variant_a(r, run.startswith("A2")) if run.startswith("A") else r["questions"]
            status, reply, ms = post(key, {"model": MODEL, "state": r["state"], "questions": questions})
            out = {"seq": r["seq"], "task_id": r["task_id"], "step": r["step"], "run": run, "http": status, "latency_ms": ms}
            if status == 200 and isinstance(reply, dict):
                out["answers"] = reply.get("answers")
                out["usage"] = reply.get("usage")
                out["served_model"] = reply.get("model")
            else:
                out["error"] = reply if isinstance(reply, str) else json.dumps(reply)[:500]
            with lock:
                state["spent"] += float(((out.get("usage") or {}).get("cost")) or 0.0)
            return out

        with path.open("a", encoding="utf-8") as f, cf.ThreadPoolExecutor(args.workers) as pool:
            for out in pool.map(one, todo):
                if out is None:
                    continue
                f.write(json.dumps(out, ensure_ascii=False) + "\n")
                f.flush()
        sent = len(todo) - state["skipped"]
        print(f"{run}: {sent} sent, {state['skipped']} not sent for the budget, spent so far {state['spent']:.4f}", flush=True)
        state["skipped"] = 0
        if state["spent"] >= args.budget:
            # With several workers, requests already in flight when the cap was reached still finish and are paid.
            print("budget reached, stopping", flush=True)
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
