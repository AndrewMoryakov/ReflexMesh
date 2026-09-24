"""Run the V0.3 routing experiment: rules, Jev (via ReflexMesh + JevRouter) and an LLM via pi.

Writes one JSON line per decision to <output-dir>/decisions.jsonl plus metadata.json.
Does not score; see scripts/v03_score.py. No retries; failures are recorded as errors.
"""

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "experiments" / "v03"))
from reflexmesh.routing.jev_router import DESCRIPTIONS  # noqa: E402
from reflexmesh.contracts.task import Route  # noqa: E402
import rules  # noqa: E402

KEY_NAMES = {"TYPESAFE_API_KEY", "JEV_API_KEY", "OPENROUTER_API_KEY"}
EXP = ROOT / "experiments" / "v03"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def load_dataset(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def eligible(case: dict) -> list[str]:
    return [r.value for r in Route if r.value in case["capabilities"] and r.value in case["allowed_routes"]]


def run_rules(case: dict) -> dict:
    start = time.monotonic()
    chosen = rules.route(case["goal"], eligible(case))
    return {"status": "selected" if chosen else "abstained", "route": chosen, "reason": None,
            "latency_ms": round((time.monotonic() - start) * 1000, 3), "cost": 0.0, "calls": 0}


def snapshot(decisions: Path) -> set[str]:
    return {p.name for p in decisions.glob("*.json")} if decisions.is_dir() else set()


def run_jev(case: dict, args: argparse.Namespace, work: Path) -> dict:
    task = {"schema_version": "0.1", "task_id": case["case_id"], "goal": case["goal"],
            "capabilities": case["capabilities"], "allowed_routes": case["allowed_routes"]}
    task_path = work / "task.json"
    task_path.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in KEY_NAMES}
    env["PYTHONPATH"] = str(ROOT / "src")
    decisions = args.upstream_dir / ".jevrouter" / "decisions"
    before = snapshot(decisions)
    start = time.monotonic()
    try:
        # V0.3 was measured without the NONE candidate (ADR-0003); keep the frozen protocol reproducible.
        done = subprocess.run([sys.executable, "-m", "reflexmesh", "route", "--provider", "jevrouter",
                               "--no-none-candidate",
                               "--jev-url", args.jev_url, "--timeout", str(args.timeout),
                               "--input", str(task_path)],
                              cwd=ROOT, env=env, capture_output=True, timeout=args.timeout + 10, check=False)
        stdout = done.stdout
    except subprocess.TimeoutExpired:
        stdout = b""
    latency = round((time.monotonic() - start) * 1000, 3)
    new = sorted(snapshot(decisions) - before)
    receipt = {}
    if len(new) == 1:
        try:
            receipt = json.loads((decisions / new[0]).read_bytes())
        except ValueError:
            receipt = {}
    raw_jev = receipt.get("raw_jev") if isinstance(receipt.get("raw_jev"), dict) else {}
    usage = raw_jev.get("usage") if isinstance(raw_jev.get("usage"), dict) else {}
    stages = receipt.get("raw_jev_stages")
    try:
        result = json.loads(stdout)
    except ValueError:
        result = {}
    status = result.get("status")
    mapped = {"selected": "selected", "abstained": "abstained",
              "needs_confirmation": "abstained", "failed": "error"}.get(status, "error")
    return {"status": mapped, "route": result.get("route") if mapped == "selected" else None,
            "reason": result.get("reason_code"), "latency_ms": latency,
            "cost": usage.get("cost"), "calls": len(stages) if isinstance(stages, list) and stages else 1,
            "confidence": result.get("confidence"),
            "probabilities": ((result.get("trace") or {}).get("upstream") or {}).get("probabilities"),
            "decision_id": receipt.get("decision_id"), "generation_id": raw_jev.get("id"),
            "model": raw_jev.get("model")}


def llm_prompt(case: dict) -> tuple[str, str]:
    text = (EXP / "llm_prompt.md").read_text(encoding="utf-8")
    system = text.split("<!-- system -->", 1)[1].split("<!-- user -->", 1)[0].strip()
    user = text.split("<!-- user -->", 1)[1].strip()
    candidates = "\n".join(f"- id: {r}\n  description: {DESCRIPTIONS[Route(r)]}" for r in eligible(case))
    return system, user.replace("{goal}", case["goal"]).replace("{candidates}", candidates)


def parse_llm(text: str, allowed: list[str]) -> tuple[str, str | None, str | None]:
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        value = json.loads(cleaned)
        chosen = value["route"] if isinstance(value, dict) else None
    except (ValueError, KeyError):
        return "error", None, "invalid_output"
    if chosen == "ABSTAIN":
        return "abstained", None, "llm_abstain"
    if not isinstance(chosen, str):
        return "error", None, "invalid_output"
    # Out-of-set answers are kept as selected so scoring can count them as forbidden.
    return "selected", chosen, None if chosen in allowed else "outside_candidates"


def run_llm(case: dict, args: argparse.Namespace, work: Path) -> dict:
    system, user = llm_prompt(case)
    command = ["pi", "-p", "--mode", "json", "--provider", args.llm_provider, "--model", args.llm_model,
               "--no-tools", "--no-extensions", "--no-skills", "--no-context-files", "--no-session",
               "--system-prompt", system, user]
    start = time.monotonic()
    try:
        done = subprocess.run(command, cwd=work, capture_output=True, timeout=args.timeout, check=False)
        stdout = done.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        stdout = ""
    latency = round((time.monotonic() - start) * 1000, 3)
    message = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "message_end" and (event.get("message") or {}).get("role") == "assistant":
            message = event["message"]
    if message is None:
        return {"status": "error", "route": None, "reason": "no_output", "latency_ms": latency,
                "cost": None, "calls": 1}
    text = "".join(c.get("text", "") for c in message.get("content", []) if c.get("type") == "text")
    status, chosen, reason = parse_llm(text, eligible(case))
    usage = message.get("usage") or {}
    return {"status": status, "route": chosen, "reason": reason, "latency_ms": latency,
            "cost": (usage.get("cost") or {}).get("total"), "calls": 1, "raw_text": text[:200],
            "model": message.get("model"), "stop_reason": message.get("stopReason")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--comparators", default="rules,jev,llm")
    parser.add_argument("--dataset", type=Path, default=EXP / "dataset.jsonl")
    parser.add_argument("--jev-url", default="http://127.0.0.1:8787")
    parser.add_argument("--upstream-dir", type=Path)
    parser.add_argument("--llm-provider", default="openai-codex")
    parser.add_argument("--llm-model", default="gpt-5.5")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    protocol = json.loads((EXP / "protocol.json").read_text(encoding="utf-8"))
    comparators = [c for c in args.comparators.split(",") if c]
    if "jev" in comparators and args.upstream_dir is None:
        parser.error("--upstream-dir is required for jev")
    out = args.output_dir.resolve()
    if out.is_relative_to(ROOT) or out.exists():
        parser.error("output directory must be new and outside the repository")
    out.mkdir(parents=True)
    work = out / "work"
    work.mkdir()
    dataset = load_dataset(args.dataset)
    meta = {"reflexmesh_commit": git(ROOT, "rev-parse", "HEAD"),
            "reflexmesh_dirty": bool(git(ROOT, "status", "--porcelain")),
            "protocol_status": protocol["status"], "python": sys.version.split()[0],
            "platform": platform.platform(), "comparators": comparators, "cases": len(dataset),
            "jev_url": args.jev_url if "jev" in comparators else None,
            "jevrouter_commit": git(args.upstream_dir, "rev-parse", "HEAD") if args.upstream_dir else None,
            "llm": f"{args.llm_provider}/{args.llm_model}" if "llm" in comparators else None,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (out / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    spent = {"jev": 0.0, "llm_calls": 0}
    with (out / "decisions.jsonl").open("a", encoding="utf-8") as sink:
        for comparator in comparators:
            for run in range(1, protocol["repeats"][comparator] + 1):
                for case in dataset:
                    if comparator == "jev" and spent["jev"] > protocol["budgets"]["jev_usd"]:
                        parser.exit(3, "jev budget exhausted\n")
                    if comparator == "llm" and spent["llm_calls"] >= protocol["budgets"]["llm_calls"]:
                        parser.exit(3, "llm call budget exhausted\n")
                    if comparator == "rules":
                        record = run_rules(case)
                    elif comparator == "jev":
                        record = run_jev(case, args, work)
                        spent["jev"] += record.get("cost") or 0
                    else:
                        record = run_llm(case, args, work)
                        spent["llm_calls"] += 1
                    record.update(comparator=comparator, run=run, case_id=case["case_id"])
                    sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                    sink.flush()
                    print(f"{comparator} run{run} {case['case_id']}: {record['status']} {record['route']}",
                          flush=True)
    meta["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (out / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
