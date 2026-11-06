"""Numbers in V0.6-M5-results-2026-11-06.md, recomputed from the rows in this folder.

    python analysis.py
"""
import collections
import json
from pathlib import Path

HERE = Path(__file__).parent
rows = [json.loads(line) for line in (HERE / "results.jsonl").read_text("utf-8").splitlines()]
disrupted = [json.loads(line) for line in (HERE / "disrupted-results.all-270.jsonl").read_text("utf-8").splitlines()]
MODES = ("J0", "J1", "L")


def ok(row):
    return row["oracle_success"] is True


print("per task, confirmed out of 3 (J0 J1 L)")
for task in dict.fromkeys(r["task"] for r in rows):
    mine = [r for r in rows if r["task"] == task]
    print(f"  {task:20} {mine[0]['group']:22}", *(sum(ok(r) for r in mine if r["mode"] == m) for m in MODES))

for m in MODES:
    mine = [r for r in rows if r["mode"] == m]
    print(m, "confirmed", sum(map(ok, mine)), "of", len(mine),
          "by repeat", [sum(ok(r) for r in mine if r["repeat"] == k) for k in (1, 2, 3)],
          "claimed finish", sum(r["claimed_finish"] for r in mine),
          "confirmed without a claim", sum(ok(r) and not r["claimed_finish"] for r in mine),
          "false claims", [(r["task"], r["repeat"], r["oracle_reason"]) for r in mine if r["false_claim"]],
          "stop reasons", dict(collections.Counter(r["stop_reason"] for r in mine)),
          "cost", round(sum(r["cost"] for r in mine), 4))

j1 = [r for r in rows if r["mode"] == "J1"]
alone = [r for r in j1 if not r.get("assist_calls")]
print("J1 without any assistance call", len(alone), "confirmed", sum(map(ok, alone)))
print("J1 with assistance", len(j1) - len(alone), "confirmed", sum(ok(r) for r in j1 if r.get("assist_calls")))
pairs = collections.Counter()
for r in j1:
    twin = next(x for x in rows if x["mode"] == "J0" and x["task"] == r["task"] and x["repeat"] == r["repeat"])
    pairs[f"J0 {'ok' if ok(twin) else 'no'} / J1 {'ok' if ok(r) else 'no'}"] += 1
print("same task and repeat:", dict(pairs))
print("over the 240 s wall limit:", [(r["task"], r["mode"], r["repeat"], r["wall_seconds"]) for r in rows if r["wall_seconds"] > 240])
print("no oracle verdict:", [(r["task"], r["mode"], r["repeat"]) for r in rows if r["oracle_success"] is None])

print("first repeat 3 (set aside):")
for m in MODES:
    mine = [r for r in disrupted if r["repeat"] == 3 and r["mode"] == m]
    print(f"  {m} ran {sum(r['category'] == 'ran' for r in mine)} infra {sum(r['category'] == 'infra' for r in mine)} "
          f"confirmed {sum(map(ok, mine))}; re-run confirmed {sum(ok(r) for r in rows if r['repeat'] == 3 and r['mode'] == m)}")
