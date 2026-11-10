"""Send browser steps of `Mlchemist/myjev-browser-decisions` to decision models on OpenRouter (see PROTOCOL.md).

Standard library only. The OpenRouter key is read from the first line of stdin; it is never an argument,
never printed and never written. One JSONL file per model under --out; a step already answered (or
finally refused) in that file is skipped, so a stopped run continues where it ended.

  python run_openrouter.py --data benchmark.jsonl --ids ids.txt --sample pilot --models m1,m2 \
      --prices prices.json --out results/ --budget 0.30 < keyfile
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
KEY_URL = "https://openrouter.ai/api/v1/key"
TARGET_OPS = {"CLICK": "click_target", "TYPE_TEXT": "type_text_target", "SELECT": "select_target"}
KEY_FLOOR = 0.10          # stop when the key has less than this left (USD)
ATTEMPTS = 3


def sample_ids(ids: list[str], which: str) -> list[str]:
    ordered = sorted(set(ids))
    pilot = random.Random(1).sample(ordered, 30)
    if which == "pilot":
        return pilot
    rest = sorted(set(ordered) - set(pilot))
    return random.Random(2).sample(rest, 300)


def load_rows(path: Path, wanted: set[str]) -> dict[str, dict]:
    rows = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec["meta"]["id"] in wanted:
                rows[rec["meta"]["id"]] = rec
    return rows


def slim(answer: dict | None, gold_keys: list[str] | None = None) -> dict | None:
    """What scoring needs from one answer: the choice, its probability, the stated confidence; for a
    target question also the probability the model gave the gold keys. Not the full distribution."""
    if not isinstance(answer, dict):
        return None
    probs = answer.get("probabilities") or {}
    choice = answer.get("choice")
    out = {"choice": choice, "p_choice": probs.get(choice) if isinstance(probs, dict) else None,
           "confidence": answer.get("confidence"), "n_probs": len(probs) if isinstance(probs, dict) else None}
    if gold_keys is not None and isinstance(probs, dict):
        out["p_gold"] = sum(float(probs.get(k) or 0.0) for k in gold_keys)
    return out


def post(key: str, body: dict, timeout: float = 60.0) -> tuple[int, dict | str, int, int]:
    """(http status or 0, parsed body or error text, latency ms, attempts)."""
    data = json.dumps(body).encode("utf-8")
    delay, last = 2.0, (0, "not sent", 0)
    for attempt in range(1, ATTEMPTS + 1):
        req = urllib.request.Request(ENDPOINT, data=data, method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/AndrewMoryakov/ReflexMesh", "X-Title": "ReflexMesh decision-model eval"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", "replace")
                ms = int((time.monotonic() - t0) * 1000)
                try:
                    return r.status, json.loads(text), ms, attempt
                except ValueError:
                    return r.status, "not json: " + text[:300], ms, attempt
        except urllib.error.HTTPError as e:
            ms = int((time.monotonic() - t0) * 1000)
            text = e.read().decode("utf-8", "replace")[:600]
            last = (e.code, text, ms)
            if e.code != 429 and e.code < 500:
                return e.code, text, ms, attempt
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = (0, f"{type(e).__name__}: {e}"[:300], int((time.monotonic() - t0) * 1000))
        time.sleep(delay)
        delay *= 2
    return last[0], last[1], last[2], ATTEMPTS


def key_remaining(key: str) -> float | None:
    req = urllib.request.Request(KEY_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read()).get("data") or {}
        rem = d.get("limit_remaining")
        return None if rem is None else float(rem)
    except (urllib.error.URLError, ValueError, OSError):
        return None


def one(key: str, model: str, rec: dict) -> dict:
    gold = rec["gold"]
    body = dict(rec["request"], model=model)
    status, reply, ms, attempts = post(key, body)
    out = {"id": rec["meta"]["id"], "source": rec["meta"].get("source"), "split": rec["meta"].get("split"),
           "model": model, "http": status, "latency_ms": ms, "attempts": attempts,
           "gold_operation": gold.get("operation"),
           "gold_target": gold.get(TARGET_OPS.get(gold.get("operation"), ""), None)}
    if status != 200 or not isinstance(reply, dict) or not isinstance(reply.get("answers"), dict):
        out["error"] = reply if isinstance(reply, str) else json.dumps(reply)[:600]
        return out
    answers = reply["answers"]
    op = slim(answers.get("operation"))
    out["operation"] = op
    chosen = (op or {}).get("choice")
    if chosen in TARGET_OPS:
        out["chosen_target"] = slim(answers.get(TARGET_OPS[chosen]))
    gq = TARGET_OPS.get(gold.get("operation"))
    if gq:
        out["gold_question"] = slim(answers.get(gq), gold.get(gq) or [])
    out["questions_asked"] = sorted(rec["request"]["questions"])
    out["questions_answered"] = sorted(answers)
    usage = reply.get("usage") or {}
    out["usage"] = {k: usage.get(k) for k in ("input_tokens", "output_tokens", "prompt_tokens", "cost") if k in usage}
    out["served_model"] = reply.get("model")
    out["request_id"] = reply.get("id")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--ids", required=True, type=Path)
    ap.add_argument("--sample", choices=("pilot", "main"), required=True)
    ap.add_argument("--models", required=True)
    ap.add_argument("--prices", required=True, type=Path, help="JSON: model -> USD per input token")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--budget", type=float, required=True, help="USD for this invocation, all models together")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    key = sys.stdin.readline().strip().lstrip("\ufeff")   # a PowerShell pipe may prefix a byte-order mark
    if not key:
        print("no key on stdin", file=sys.stderr)
        return 2
    prices = json.loads(args.prices.read_text("utf-8"))
    ids = sample_ids(args.ids.read_text("utf-8").split(), args.sample)
    rows = load_rows(args.data, set(ids))
    missing = [i for i in ids if i not in rows]
    if missing:
        print(f"{len(missing)} sampled ids are not in the data file", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"sample-{args.sample}.txt").write_text("\n".join(ids) + "\n", "utf-8")

    spent, lock = 0.0, threading.Lock()
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        path = args.out / (model.replace("/", "__").replace("~", "latest-") + f".{args.sample}.jsonl")
        done = set()
        if path.exists():
            done = {json.loads(l)["id"] for l in path.read_text("utf-8").splitlines() if l.strip()}
        todo = [rows[i] for i in ids if i not in done]
        remaining = key_remaining(key)
        print(f"{model}: {len(todo)} to send, key remaining {remaining}", flush=True)
        if remaining is not None and remaining < KEY_FLOOR:
            print("key below floor, stopping", flush=True)
            break
        price = float(prices.get(model, 0.0))
        stop = threading.Event()
        with path.open("a", encoding="utf-8") as f, cf.ThreadPoolExecutor(args.workers) as pool:
            def run(rec):
                if stop.is_set():
                    return None
                return one(key, model, rec)
            for res in pool.map(run, todo):
                if res is None:
                    continue
                usage = res.get("usage") or {}
                cost = usage.get("cost")
                if cost is None:
                    cost = float(usage.get("input_tokens") or usage.get("prompt_tokens") or 0) * price
                res["cost_usd"] = float(cost or 0.0)
                with lock:
                    spent += res["cost_usd"]
                    f.write(json.dumps(res, ensure_ascii=False) + "\n")
                    f.flush()
                    if spent > args.budget:
                        stop.set()
        print(f"{model}: done, spent so far {spent:.4f}", flush=True)
        if spent > args.budget:
            print("budget reached, stopping", flush=True)
            break
    print(f"total spent {spent:.4f} USD (estimate from usage)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
