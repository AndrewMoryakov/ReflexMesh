"""Run the V0.3b refusal experiment: Jev candidate-set variants plus the V0.3 LLM reference.

Jev variants call JevRouter POST /route directly with the adapter's candidate format:
  J0  allowed candidates only (current ReflexMesh adapter behaviour)
  J1  allowed candidates + NONE
  J2  all declared capabilities; a choice outside allowed_routes becomes an abstention
  J3  all declared capabilities + NONE; NONE or a choice outside allowed_routes abstains
Writes one JSON line per decision to <output-dir>/decisions.jsonl. No retries.
"""

import argparse
import http.client
import json
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
import v03_run  # noqa: E402
from reflexmesh.routing.jev_router import DESCRIPTIONS, validate_config  # noqa: E402
from reflexmesh.contracts.task import Route  # noqa: E402

EXP = ROOT / "experiments" / "v03b"
NONE_ID = "NONE"
NONE_DESCRIPTION = ("None of the other listed capabilities fits this request: it needs a capability "
                    "that is not listed or is outside their scope. Choose this instead of a poor match.")
VARIANTS = {"J0": (False, False), "J1": (False, True), "J2": (True, False), "J3": (True, True)}


def candidate(cid: str, description: str) -> dict:
    # Same shape as reflexmesh.routing.jev_router.route_task.
    return {"id": cid, "name": cid, "type": "subagent", "description": description,
            "verification": {"status": "unverified", "source": "reflexmesh_declared"},
            "availability": {"available": True},
            "risk": {"level": "low", "categories": ["routing_only"]},
            "execution": {"mode": "custom", "dry_run": True}}


def candidate_ids(case: dict, variant: str) -> list[str]:
    catalog, none = VARIANTS[variant]
    source = case["capabilities"] if catalog else case["allowed_routes"]
    ids = [r.value for r in Route if r.value in case["capabilities"] and r.value in source]
    return ids + [NONE_ID] if none else ids


def payload(case: dict, variant: str) -> bytes:
    cands = [candidate(cid, NONE_DESCRIPTION if cid == NONE_ID else DESCRIPTIONS[Route(cid)])
             for cid in candidate_ids(case, variant)]
    return json.dumps({"request": case["goal"], "actor_permissions": [], "candidates": cands},
                      ensure_ascii=True).encode()


def interpret(data: dict, case: dict, sent: list[str]) -> dict:
    """Map an upstream response to a final ReflexMesh-style outcome for this variant."""
    decision = data.get("decision") if isinstance(data.get("decision"), dict) else {}
    upstream_status = data.get("status")
    choice = decision.get("selected")
    raw = data.get("raw_jev") if isinstance(data.get("raw_jev"), dict) else {}
    answers = raw.get("answers") if isinstance(raw.get("answers"), dict) else {}
    tool = answers.get("tool") if isinstance(answers.get("tool"), dict) else {}
    base = {"upstream_status": upstream_status, "jev_choice": decision.get("jev_choice"),
            "confidence": tool.get("confidence"), "probabilities": tool.get("probabilities"),
            "cost": (raw.get("usage") or {}).get("cost") if isinstance(raw.get("usage"), dict) else None,
            "decision_id": data.get("decision_id"), "generation_id": raw.get("id"),
            "provider": (data.get("provenance") or {}).get("jev_provider")}
    if data.get("error") or upstream_status not in ("selected", "needs_confirmation", "no_decision"):
        return dict(base, status="error", route=None, reason="provider_error")
    if upstream_status != "selected":
        return dict(base, status="abstained", route=None, reason=f"upstream_{upstream_status}")
    if choice not in sent:
        return dict(base, status="error", route=None, reason="invalid_response")
    if choice == NONE_ID:
        return dict(base, status="abstained", route=None, reason="none_selected")
    if choice not in case["allowed_routes"]:
        return dict(base, status="abstained", route=None, reason="selected_not_allowed")
    return dict(base, status="selected", route=choice, reason="upstream_selected")


def run_jev(case: dict, variant: str, host: str, port: int, timeout: float) -> dict:
    sent = candidate_ids(case, variant)
    body = payload(case, variant)
    start = time.monotonic()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("POST", "/route", body=body,
                         headers={"Content-Type": "application/json", "Accept": "application/json"})
            resp = conn.getresponse()
            raw = resp.read(1024 * 1024 + 1)
            status_code = resp.status
        finally:
            conn.close()
        data = json.loads(raw) if status_code == 200 else {}
        result = interpret(data, case, sent) if data else {"status": "error", "route": None,
                                                           "reason": f"http_{status_code}"}
    except (OSError, ValueError, http.client.HTTPException) as exc:
        result = {"status": "error", "route": None, "reason": type(exc).__name__}
    result.update(latency_ms=round((time.monotonic() - start) * 1000, 3), calls=1, candidates=sent)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--comparators", default="J0,J1,J2,J3,llm")
    parser.add_argument("--dataset", type=Path, default=EXP / "dataset.jsonl")
    parser.add_argument("--jev-url", default="http://127.0.0.1:8787")
    parser.add_argument("--llm-provider", default="openai-codex")
    parser.add_argument("--llm-model", default="gpt-5.5")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    protocol = json.loads((EXP / "protocol.json").read_text(encoding="utf-8"))
    host, port = validate_config(args.jev_url, args.timeout)
    comparators = [c for c in args.comparators.split(",") if c]
    out = args.output_dir.resolve()
    if out.is_relative_to(ROOT) or out.exists():
        parser.error("output directory must be new and outside the repository")
    out.mkdir(parents=True)
    (out / "work").mkdir()
    dataset = v03_run.load_dataset(args.dataset)
    meta = {"reflexmesh_commit": v03_run.git(ROOT, "rev-parse", "HEAD"),
            "reflexmesh_dirty": bool(v03_run.git(ROOT, "status", "--porcelain")),
            "protocol_status": protocol["status"], "python": sys.version.split()[0],
            "platform": platform.platform(), "comparators": comparators, "cases": len(dataset),
            "jev_url": args.jev_url, "llm": f"{args.llm_provider}/{args.llm_model}",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (out / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    spent = {"jev": 0.0, "llm": 0}
    llm_args = argparse.Namespace(llm_provider=args.llm_provider, llm_model=args.llm_model, timeout=args.timeout)
    with (out / "decisions.jsonl").open("a", encoding="utf-8") as sink:
        for comparator in comparators:
            for run in range(1, protocol["repeats"] + 1):
                for case in dataset:
                    if comparator == "llm":
                        if spent["llm"] >= protocol["budgets"]["llm_calls"]:
                            parser.exit(3, "llm call budget exhausted\n")
                        record = v03_run.run_llm(case, llm_args, out / "work")
                        spent["llm"] += 1
                    else:
                        if spent["jev"] > protocol["budgets"]["jev_usd"]:
                            parser.exit(3, "jev budget exhausted\n")
                        record = run_jev(case, comparator, host, port, args.timeout)
                        spent["jev"] += record.get("cost") or 0
                    record.update(comparator=comparator, run=run, case_id=case["case_id"])
                    sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                    sink.flush()
                    print(f"{comparator} run{run} {case['case_id']}: {record['status']} {record['route']}"
                          f" {record.get('reason')}", flush=True)
    meta["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (out / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
