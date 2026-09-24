"""Collect a V0.2 live-routing evidence packet from a running pinned JevRouter.

The JevRouter process owns the provider key. This script never starts a provider,
passes a key to ReflexMesh, retries a call, or claims to prove a model invocation.
"""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from reflexmesh.routing.jev_router import validate_config  # noqa: E402

PIN = "f944acb6530621bced023352e2358a63218bf4d9"
PROVIDERS = {"typesafe": "typesafe", "openrouter": "openrouter:~typesafe/jev-latest"}
KEY_NAMES = {"TYPESAFE_API_KEY", "JEV_API_KEY", "OPENROUTER_API_KEY"}
GOAL = ("Составь новую короткую фразу приветствия для уведомления. "
        "Готового текста и изображения нет; интерфейс открывать не нужно.")
ROUTES = ["CUA", "LLM", "PERCEPTION"]
CASES = {
    "multi": ROUTES,
    "filtered": ["LLM", "PERCEPTION"],
    "empty": [],
}


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def checked_receipt(result: dict, decisions: Path, evidence: Path,
                    expected_ids: set[str]) -> dict[str, bool]:
    checks = {"receipt_found": False, "response_hash_matches": False,
              "candidate_set_matches": False, "decision_matches": False}
    trace = result.get("trace") or {}
    upstream = trace.get("upstream") or {}
    decision_id = upstream.get("decision_id")
    if not isinstance(decision_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", decision_id):
        return checks
    receipt = decisions / f"{decision_id}.json"
    if not receipt.is_file():
        return checks
    raw = receipt.read_bytes()
    (evidence / "decisions").mkdir(exist_ok=True)
    (evidence / "decisions" / receipt.name).write_bytes(raw)
    checks["receipt_found"] = True
    checks["response_hash_matches"] = hashlib.sha256(raw).hexdigest() == trace.get("response_sha256")
    try:
        saved = json.loads(raw)
        rows = saved["decision"]["candidates"]
        ids = [row["id"] for row in rows]
        checks["candidate_set_matches"] = len(ids) == len(expected_ids) and set(ids) == expected_ids
        checks["decision_matches"] = (saved["decision_id"] == decision_id
                                       and saved["decision"]["selected"] == upstream.get("selected")
                                       and saved["provenance"]["jev_provider"] == upstream.get("provider"))
    except (ValueError, TypeError, KeyError):
        pass
    return checks


def collect_case(name: str, allowed: list[str], args: argparse.Namespace,
                 evidence: Path, decisions: Path) -> dict:
    task = {"schema_version": "0.1", "task_id": f"v02-live-{name}", "goal": GOAL,
            "capabilities": ROUTES, "allowed_routes": allowed}
    task_path = evidence / f"{name}.input.json"
    save_json(task_path, task)
    env = {key: value for key, value in os.environ.items() if key not in KEY_NAMES}
    env["PYTHONPATH"] = str(ROOT / "src")
    command = [sys.executable, "-m", "reflexmesh", "route", "--provider", "jevrouter",
               "--jev-url", args.jev_url, "--input", str(task_path)]
    before = len(list(decisions.glob("*.json"))) if name == "empty" else None
    try:
        completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, timeout=180,
                                   check=False)
        exit_code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code, stdout, stderr = None, exc.stdout or b"", exc.stderr or b""
        stderr += b"\nCollector stopped waiting after 180 seconds. Upstream may still be processing.\n"
    (evidence / f"{name}.stdout.json").write_bytes(stdout)
    (evidence / f"{name}.stderr.txt").write_bytes(stderr)
    try:
        result = json.loads(stdout)
        if not isinstance(result, dict):
            result = {}
    except (ValueError, UnicodeError):
        result = {}
    trace = result.get("trace") or {}
    expected = set(allowed)
    eligible = result.get("eligible_routes")
    checks = {
        "cli_response": bool(result),
        "eligible_routes": (isinstance(eligible, list) and len(eligible) == len(expected)
                            and set(eligible) == expected),
        "not_demo": result.get("is_stub") is False,
        "no_execution": result.get("execution_performed") is False,
    }
    if name == "multi":
        checks["selected_multi_candidate"] = (exit_code == 0 and result.get("status") == "selected"
                                              and result.get("route") in expected)
    elif name == "filtered":
        checks["forbidden_route_excluded"] = (result.get("route") != "CUA"
                                              and result.get("status") in ("selected", "abstained", "needs_confirmation")
                                              and exit_code in (0, 3, 4))
    else:
        checks["empty_is_local_abstention"] = (exit_code == 3 and result.get("status") == "abstained"
                                               and result.get("reason_code") == "no_eligible_route"
                                               and trace.get("request_sent") is False
                                               and len(list(decisions.glob("*.json"))) == before)
    if name != "empty":
        checks["request_sent"] = trace.get("request_sent") is True
        checks.update(checked_receipt(result, decisions, evidence, expected))
        checks["provider_matches"] = (trace.get("upstream") or {}).get("provider") == PROVIDERS[args.provider]
        if name == "multi":
            checks["selected_matches_receipt"] = (result.get("route")
                                                    == (trace.get("upstream") or {}).get("selected"))
    return {"exit_code": exit_code, "checks": checks, "passed": all(checks.values()),
            "decision_id": (trace.get("upstream") or {}).get("decision_id")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-dir", required=True, type=Path,
                        help="pinned JevRouter checkout and server working directory")
    parser.add_argument("--provider", required=True, choices=tuple(PROVIDERS))
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="new evidence directory outside the ReflexMesh repository")
    parser.add_argument("--jev-url", default="http://127.0.0.1:8787")
    args = parser.parse_args()
    host, port = validate_config(args.jev_url, 30)
    upstream = args.upstream_dir.resolve()
    evidence = args.output_dir.resolve()
    if evidence.is_relative_to(ROOT) or evidence.is_relative_to(upstream):
        parser.error("output directory must be outside both repositories")
    if evidence.exists():
        parser.error("output directory already exists")
    if git(upstream, "rev-parse", "HEAD") != PIN:
        parser.error("unexpected JevRouter revision; inspect compatibility before running")
    decisions = upstream / ".jevrouter" / "decisions"
    try:
        connection = http.client.HTTPConnection(host, port, timeout=3)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("non-200 health response")
            raw = response.read(4097)
            if len(raw) > 4096:
                raise ValueError("oversized health response")
            health = json.loads(raw)
        finally:
            connection.close()
    except (OSError, ValueError, http.client.HTTPException) as exc:
        parser.error(f"JevRouter is not responding on loopback: {type(exc).__name__}")
    if not isinstance(health, dict) or health.get("provider") != PROVIDERS[args.provider]:
        parser.error("JevRouter health does not report the requested real provider")
    evidence.mkdir(parents=True, mode=0o700)
    metadata = {"reflexmesh_commit": git(ROOT, "rev-parse", "HEAD"),
                "reflexmesh_dirty": bool(git(ROOT, "status", "--porcelain")),
                "jevrouter_commit": PIN, "provider": health["provider"],
                "python": sys.version.split()[0], "platform": platform.platform(),
                "node": subprocess.check_output(["node", "--version"], text=True).strip()}
    policy = upstream / ".jevrouter" / "policy.json"
    metadata["policy_sha256"] = hashlib.sha256(policy.read_bytes()).hexdigest() if policy.is_file() else None
    save_json(evidence / "metadata.json", metadata)
    cases = {name: collect_case(name, allowed, args, evidence, decisions)
             for name, allowed in CASES.items()}
    report = {"mechanical_checks_passed": all(item["passed"] for item in cases.values()),
              "live_gate_closed": False,
              "manual_evidence_needed": ["confirm server process and policy match the pinned checkout",
                                         "confirm actual provider/model invocation and configuration",
                                         "confirm tested ReflexMesh checkout corresponds to recorded commit",
                                         "review upstream receipts and redact before sharing",
                                         "record pass/fail for every V0.2 live-package item"],
              "cases": cases}
    save_json(evidence / "report.json", report)
    print(json.dumps({"evidence_dir": str(evidence), "mechanical_checks_passed": report["mechanical_checks_passed"],
                      "live_gate_closed": False}))
    return 0 if report["mechanical_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
