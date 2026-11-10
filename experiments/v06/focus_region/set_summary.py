"""Fingerprint and composition of a recorded step set: SHA-256 over the files (name, newline, bytes) in name order.

  python set_summary.py STEPS_DIR
"""
import sys, json, glob, hashlib, os, collections, statistics
files = sorted(glob.glob(sys.argv[1] + "/*.steps.jsonl"))
h = hashlib.sha256()
for f in files:
    h.update(os.path.basename(f).encode() + b"\n"); h.update(open(f, "rb").read())
rows = [json.loads(l) for f in files for l in open(f, encoding="utf-8") if l.strip()]
ops = collections.Counter(); auto = collections.Counter(); target, dup, sizes = 0, 0, []
for r in rows:
    act = r["gold"]["next_action"][0]; ops[act] += 1
    if r.get("auto"): auto[r["auto"]] += 1
    for k, keys in r["gold"].items():
        if k == "next_action" or k.endswith("__value") or k.endswith("__direction"): continue
        crit = r["questions"][k]["criteria"]; target += 1; sizes.append(len(crit))
        labels = [str(crit[x]).split(" →")[0] for x in keys]
        if any(sum(1 for d in crit.values() if str(d).split(" →")[0] == lab) > 1 for lab in labels): dup += 1
print("files", len(files), "rows", len(rows), "sha256", h.hexdigest())
print("gold actions", dict(ops), "| auto", dict(auto))
print("target choices", target, "| with a repeated description", dup, "| options per target question: median", statistics.median(sizes), "max", max(sizes), "over 50:", sum(s > 50 for s in sizes))
