"""Collect extended V0.2 live-routing evidence from a running pinned JevRouter.

Complements collect_v02_live.py (the gate packet) with broader live scenarios:
route-specific goals in two languages, two-candidate sets, repeatability of one
request and, as a separate suite, a live provider failure. Mechanical checks
verify the ReflexMesh contract against upstream receipts. The ``hint_route`` of a
case is an informal expectation recorded as an observation only; it is not a
V0.3 label and never affects ``mechanical_checks_passed``.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_v02_live as base  # noqa: E402

EXIT_CODES = {"selected": 0, "abstained": 3, "needs_confirmation": 4, "failed": 5}
ALL = base.ROUTES
GOALS = {
    "llm_ru": base.GOAL,
    "cua_ru": ("Открой в браузере страницу настроек профиля и нажми кнопку «Сохранить». "
               "Все данные уже заполнены; сочинять текст не нужно."),
    "perception_ru": ("Прочитай текст сообщения об ошибке на уже сделанном скриншоте окна. "
                      "Взаимодействовать с интерфейсом не нужно."),
    "llm_en": ("Write a new short greeting line for a notification. "
               "There is no ready text or image; no UI needs to be opened."),
    "cua_en": ("Open the profile settings page in the browser and click the Save button. "
               "All data is already filled in; no text needs to be written."),
    "perception_en": ("Read the error message text from an existing screenshot of the window. "
                      "No interaction with the UI is needed."),
}
# name -> (goal key, allowed routes, informal hint route, repetitions)
SUITES = {
    "main": {
        "llm_ru_all": ("llm_ru", ALL, "LLM", 1),
        "cua_ru_all": ("cua_ru", ALL, "CUA", 1),
        "perception_ru_all": ("perception_ru", ALL, "PERCEPTION", 1),
        "llm_en_all": ("llm_en", ALL, "LLM", 1),
        "cua_en_all": ("cua_en", ALL, "CUA", 1),
        "perception_en_all": ("perception_en", ALL, "PERCEPTION", 1),
        "pair_cua_perception": ("perception_ru", ["CUA", "PERCEPTION"], "PERCEPTION", 1),
        "pair_llm_cua": ("cua_ru", ["LLM", "CUA"], "CUA", 1),
        "repeat_llm_ru": ("llm_ru", ALL, "LLM", 5),
    },
    # Run against a JevRouter started with an invalid provider key.
    "provider-error": {
        "provider_error": ("llm_ru", ALL, None, 1),
    },
}


def read_health(jev_url: str) -> dict | None:
    host, port = base.validate_config(jev_url, 30)
    connection = base.http.client.HTTPConnection(host, port, timeout=3)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        raw = response.read(4097)
        if response.status != 200 or len(raw) > 4096:
            return None
        health = json.loads(raw)
        return health if isinstance(health, dict) else None
    except (OSError, ValueError, base.http.client.HTTPException):
        return None
    finally:
        connection.close()


def snapshot(decisions: Path) -> set[str]:
    return {path.name for path in decisions.glob("*.json")} if decisions.is_dir() else set()


def run_request(name: str, goal: str, allowed: list[str], jev_url: str,
                evidence: Path, decisions: Path) -> tuple[int | None, dict, list[str]]:
    task = {"schema_version": "0.1", "task_id": f"v02-ext-{name}", "goal": goal,
            "capabilities": ALL, "allowed_routes": allowed}
    task_path = evidence / f"{name}.input.json"
    base.save_json(task_path, task)
    env = {key: value for key, value in os.environ.items() if key not in base.KEY_NAMES}
    env["PYTHONPATH"] = str(base.ROOT / "src")
    command = [sys.executable, "-m", "reflexmesh", "route", "--provider", "jevrouter",
               "--jev-url", jev_url, "--input", str(task_path)]
    before = snapshot(decisions)
    try:
        completed = base.subprocess.run(command, cwd=base.ROOT, env=env, capture_output=True,
                                        timeout=180, check=False)
        exit_code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    except base.subprocess.TimeoutExpired as exc:
        exit_code, stdout, stderr = None, exc.stdout or b"", exc.stderr or b""
        stderr += b"\nCollector stopped waiting after 180 seconds.\n"
    (evidence / f"{name}.stdout.json").write_bytes(stdout)
    (evidence / f"{name}.stderr.txt").write_bytes(stderr)
    try:
        result = json.loads(stdout)
        result = result if isinstance(result, dict) else {}
    except (ValueError, UnicodeError):
        result = {}
    return exit_code, result, sorted(snapshot(decisions) - before)


def load_receipt(new: list[str], decisions: Path, evidence: Path) -> tuple[bytes | None, dict]:
    if len(new) != 1:
        return None, {}
    raw = (decisions / new[0]).read_bytes()
    (evidence / "decisions").mkdir(exist_ok=True)
    (evidence / "decisions" / new[0]).write_bytes(raw)
    try:
        saved = json.loads(raw)
        return raw, saved if isinstance(saved, dict) else {}
    except ValueError:
        return raw, {}


def observe(result: dict, saved: dict, hint: str | None) -> dict:
    upstream = (result.get("trace") or {}).get("upstream") or {}
    raw_jev = saved.get("raw_jev") if isinstance(saved.get("raw_jev"), dict) else {}
    usage = raw_jev.get("usage") if isinstance(raw_jev.get("usage"), dict) else {}
    error = saved.get("error") if isinstance(saved.get("error"), dict) else {}
    return {"status": result.get("status"), "route": result.get("route"),
            "reason_code": result.get("reason_code"), "confidence": result.get("confidence"),
            "jev_choice": upstream.get("jev_choice"), "fallback_type": upstream.get("fallback_type"),
            "probabilities": upstream.get("probabilities"),
            "elapsed_ms": (result.get("trace") or {}).get("elapsed_ms"),
            "decision_id": saved.get("decision_id"), "receipt_status": saved.get("status"),
            "upstream_error_code": error.get("code"),
            "model": raw_jev.get("model"), "generation_id": raw_jev.get("id"),
            "cost": usage.get("cost"),
            "hint_route": hint, "matches_hint": None if hint is None else result.get("route") == hint}


def check(suite: str, exit_code: int | None, result: dict, allowed: list[str],
          new: list[str], raw: bytes | None, saved: dict, provider: str) -> dict[str, bool]:
    trace = result.get("trace") or {}
    upstream = trace.get("upstream") or {}
    decision = saved.get("decision") if isinstance(saved.get("decision"), dict) else {}
    rows = decision.get("candidates") if isinstance(decision.get("candidates"), list) else []
    ids = [row.get("id") for row in rows if isinstance(row, dict)]
    status = result.get("status")
    eligible = result.get("eligible_routes")
    checks = {
        "cli_response": bool(result),
        "exit_code_matches_status": status in EXIT_CODES and exit_code == EXIT_CODES[status],
        "eligible_routes": isinstance(eligible, list) and sorted(eligible) == sorted(allowed),
        "not_demo": result.get("is_stub") is False,
        "no_execution": result.get("execution_performed") is False,
        "request_sent": trace.get("request_sent") is True,
        "one_new_receipt": len(new) == 1,
        "response_hash_matches": raw is not None and hashlib.sha256(raw).hexdigest() == trace.get("response_sha256"),
        "receipt_provider_matches": (saved.get("provenance") or {}).get("jev_provider") == provider,
        "route_within_allowed": result.get("route") is None or result.get("route") in allowed,
    }
    if suite == "provider-error":
        checks["failed_as_provider_error"] = (status == "failed" and result.get("reason_code") == "provider_error"
                                              and result.get("route") is None)
        checks["receipt_records_error"] = bool((saved.get("error") or {}).get("code"))
        checks["no_fallback_selection"] = decision.get("selected") in (None, "") and saved.get("status") != "selected"
    else:
        checks["candidate_set_matches"] = len(ids) == len(allowed) and set(ids) == set(allowed)
        checks["routing_outcome"] = status in ("selected", "abstained", "needs_confirmation")
        if status == "selected":
            checks["selected_matches_receipt"] = (result.get("route") == decision.get("selected")
                                                  == upstream.get("selected"))
            checks["decision_id_matches"] = upstream.get("decision_id") == saved.get("decision_id")
            checks["upstream_provider_matches"] = upstream.get("provider") == provider
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-dir", required=True, type=Path)
    parser.add_argument("--provider", required=True, choices=tuple(base.PROVIDERS))
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--jev-url", default="http://127.0.0.1:8787")
    parser.add_argument("--suite", choices=tuple(SUITES), default="main")
    args = parser.parse_args()
    try:
        base.validate_config(args.jev_url, 30)
    except Exception as exc:  # ValidationError: reject non-loopback or malformed URLs early
        parser.error(str(exc))
    upstream = args.upstream_dir.resolve()
    evidence = args.output_dir.resolve()
    if evidence.is_relative_to(base.ROOT) or evidence.is_relative_to(upstream):
        parser.error("output directory must be outside both repositories")
    if evidence.exists():
        parser.error("output directory already exists")
    if base.git(upstream, "rev-parse", "HEAD") != base.PIN:
        parser.error("unexpected JevRouter revision; inspect compatibility before running")
    provider = base.PROVIDERS[args.provider]
    health = read_health(args.jev_url)
    if health is None or health.get("provider") != provider:
        parser.error("JevRouter health does not report the requested real provider")
    decisions = upstream / ".jevrouter" / "decisions"
    evidence.mkdir(parents=True, mode=0o700)
    metadata = {"suite": args.suite, "reflexmesh_commit": base.git(base.ROOT, "rev-parse", "HEAD"),
                "reflexmesh_dirty": bool(base.git(base.ROOT, "status", "--porcelain")),
                "jevrouter_commit": base.PIN, "provider": health["provider"],
                "python": sys.version.split()[0], "platform": base.platform.platform(),
                "node": base.subprocess.check_output(["node", "--version"], text=True).strip()}
    base.save_json(evidence / "metadata.json", metadata)
    cases, groups = {}, {}
    for group, (goal_key, allowed, hint, repeats) in SUITES[args.suite].items():
        names = [group] if repeats == 1 else [f"{group}_{i + 1}" for i in range(repeats)]
        for name in names:
            exit_code, result, new = run_request(name, GOALS[goal_key], allowed, args.jev_url,
                                                 evidence, decisions)
            raw, saved = load_receipt(new, decisions, evidence)
            checks = check(args.suite, exit_code, result, allowed, new, raw, saved, provider)
            cases[name] = {"group": group, "goal": goal_key, "allowed": allowed, "exit_code": exit_code,
                           "checks": checks, "passed": all(checks.values()),
                           "observation": observe(result, saved, hint)}
        if repeats > 1:
            routes = [cases[name]["observation"]["route"] for name in names]
            groups[group] = {"repeats": repeats, "routes": routes,
                             "distinct_routes": sorted({str(route) for route in routes}),
                             "stable": len(set(routes)) == 1}
    report = {"suite": args.suite,
              "mechanical_checks_passed": all(item["passed"] for item in cases.values()),
              "live_gate_closed": False,
              "note": "hint_route/matches_hint are informal observations, not V0.3 labels or accuracy",
              "cases": cases, "repeat_groups": groups,
              "total_cost": sum(item["observation"]["cost"] or 0 for item in cases.values())}
    base.save_json(evidence / "report.json", report)
    print(json.dumps({"evidence_dir": str(evidence), "suite": args.suite,
                      "mechanical_checks_passed": report["mechanical_checks_passed"]}))
    return 0 if report["mechanical_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
