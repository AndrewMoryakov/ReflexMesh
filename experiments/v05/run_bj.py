"""Run the frozen B/J acceptance manifest: real Chromium, a fresh fixture per attempt.

    python experiments/v05/run_bj.py --level B --chrome /path/to/chrome --output results.json
    python experiments/v05/run_bj.py --level J --chrome ... --jev-url http://127.0.0.1:8797 --output ...

Every planned attempt is preserved: an interrupted run leaves pending rows; a failed attempt is
recorded with its reasons and never replaced. The output file must not exist yet.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1].parent
SERVER = ROOT / "experiments/v05/site/server.py"
GRACE, KILL = 5.0, 2.0
EXIT_CODES = {"completed": 0, "blocked": 3, "incomplete": 4, "failed": 5, "cancelled": 130}
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fixture_state(origin: str, timeout: float = 2.0) -> dict:
    with NO_PROXY.open(origin + "/__state", timeout=timeout) as response:
        return json.load(response)


class Fixture:
    def __init__(self, run_id: str, directory: Path, slow_seconds: float = 3.0, drop_paths=()):
        self.port, self.run_id = free_port(), run_id
        self.origin = f"http://127.0.0.1:{self.port}"
        args = [sys.executable, str(SERVER), "--port", str(self.port), "--run-id", run_id,
                "--slow-seconds", str(slow_seconds)]
        for path in drop_paths:
            args += ["--drop-path", path]
        self.log = open(directory / f"fixture-{run_id}.log", "wb")
        self.process = subprocess.Popen(args, stdout=self.log, stderr=subprocess.STDOUT)
        limit = time.monotonic() + 20.0
        while time.monotonic() < limit:
            if self.process.poll() is not None:
                raise RuntimeError(f"fixture exited with {self.process.returncode}")
            try:
                fixture_state(self.origin, 0.5)
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("fixture did not start within 20 s")

    def settled_state(self, wait: float = 6.0) -> dict:
        """Server record after any request still being handled has finished (bounded)."""
        limit = time.monotonic() + wait
        state = fixture_state(self.origin)
        while state.get("inflight") and time.monotonic() < limit:
            time.sleep(0.1)
            state = fixture_state(self.origin)
        return state

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.log.close()


def build_task(manifest: dict, case: dict, fixture: Fixture, run_id: str) -> dict:
    task = copy.deepcopy(manifest["templates"][case["template"]])
    task.update(copy.deepcopy(case.get("task", {})))
    task.update({"schema_version": "execution-task/0.1", "task_id": case["id"].replace(".", "-").lower(),
                 "revision": 1, "allowed_executors": ["browser.soh"],
                 "fixture": {"origin": fixture.origin, "run_id": run_id}})
    return task


def script_for(manifest: dict, case: dict):
    script = case.get("script")
    return manifest["scripts"][script] if isinstance(script, str) else script


def run_cli(level: str, directory: Path, task: dict, case: dict, manifest: dict, options, faults) -> dict:
    (directory / "task.json").write_text(json.dumps(task, indent=1), encoding="utf-8")
    command = [sys.executable, "-m", "reflexmesh", "run", "--input", str(directory / "task.json"),
               "--output-dir", str(directory / "out"), "--chrome", options.chrome]
    if level == "B":
        (directory / "script.json").write_text(json.dumps(script_for(manifest, case), indent=1), encoding="utf-8")
        command += ["--routing-provider", "stub", "--action-provider", "script",
                    "--script", str(directory / "script.json")]
    else:
        command += ["--routing-provider", "jevrouter", "--jev-url", options.jev_url,
                    "--action-provider", "jev"]
    if faults:
        (directory / "faults.json").write_text(json.dumps(faults, indent=1), encoding="utf-8")
        command += ["--faults", str(directory / "faults.json")]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "src"),
                                                                       os.environ.get("PYTHONPATH")]))}
    wall = task["limits"]["wall_seconds"]
    started = time.monotonic()
    with open(directory / "stdout.json", "wb") as stdout, open(directory / "stderr.log", "wb") as stderr:
        process = subprocess.run(command, cwd=directory, env=env, stdout=stdout, stderr=stderr,
                                 timeout=wall + GRACE + KILL + 60)
    elapsed = time.monotonic() - started
    raw = (directory / "stdout.json").read_text(encoding="utf-8")
    result = json.loads(raw) if raw.strip() else None
    return {"command": [Path(c).name if c == sys.executable else c for c in command], "exit_code": process.returncode,
            "process_seconds": round(elapsed, 3), "result": result}


def action(result, target_id):
    return [row for row in result.get("actions", []) if row.get("target_id") == target_id]


def evaluate(case: dict, run: dict, state: dict, task: dict, directory: Path, extra: dict) -> list[str]:
    result, failures = run["result"], []
    if result is None:
        return ["no result JSON on stdout"]
    for key, value in case["expect"].items():
        if result.get(key) != value:
            failures.append(f"{key}={result.get(key)!r} expected {value!r}")
    if run["exit_code"] != EXIT_CODES.get(result.get("attempt_status")):
        failures.append(f"exit code {run['exit_code']} does not match {result.get('attempt_status')}")
    posts = lambda path: [e for e in state.get("log", []) if e.get("method") == "POST" and e.get("path") == path]
    terminal = [row for row in result.get("trace", []) if row.get("kind") == "terminal"]
    terminal_seq = terminal[-1]["data"][2] if terminal else None
    timing = result.get("timing") or {}
    for check in case.get("checks", []):
        kind = check["check"]
        if kind == "verification":
            row = next((r for r in result["verification"] if r["id"] == check["id"]), None)
            if row is None or row["status"] != check["status"]:
                failures.append(f"verification {check['id']}={row and row['status']!r}")
        elif kind == "constraints_pass":
            bad = [r["id"] for r in result["verification"] if r["kind"] == "execution_constraint" and r["status"] != "pass"]
            if bad:
                failures.append(f"constraints not pass: {bad}")
        elif kind == "server_posts" and len(posts(check["path"])) != check["count"]:
            failures.append(f"POST {check['path']} count {len(posts(check['path']))} != {check['count']}")
        elif kind == "server_posts_max" and len(posts(check["path"])) > check["max"]:
            failures.append(f"POST {check['path']} count {len(posts(check['path']))} > {check['max']}")
        elif kind == "server_state" and state.get("state", {}).get(check["key"]) != check["value"]:
            failures.append(f"server state {check['key']}={state.get('state', {}).get(check['key'])!r}")
        elif kind == "server_form_matches_slots":
            slots = {s["id"]: s["value"] for s in task["text_slots"]}
            if state.get("state", {}).get("form") != {"name": slots.get("name"), "email": slots.get("email")}:
                failures.append("server form does not equal the declared slot values")
        elif kind == "dispatches" and result["budget"]["dispatches"] != check["count"]:
            failures.append(f"dispatches {result['budget']['dispatches']} != {check['count']}")
        elif kind == "dispatched" and not action(result, check["target_id"]):
            failures.append(f"{check['target_id']} was not dispatched")
        elif kind == "not_dispatched" and action(result, check["target_id"]):
            failures.append(f"{check['target_id']} was dispatched")
        elif kind == "returned" and not any(r.get("return_seq") for r in action(result, check["target_id"])):
            failures.append(f"{check['target_id']} has no driver return")
        elif kind == "not_returned" and any(r.get("return_seq") for r in action(result, check["target_id"])):
            failures.append(f"{check['target_id']} unexpectedly returned")
        elif kind == "effect":
            effects = [r["effect"] for r in action(result, check["target_id"])]
            if not effects or any(e not in check["in"] for e in effects):
                failures.append(f"{check['target_id']} effects {effects} not in {check['in']}")
        elif kind == "no_dispatch_after_terminal":
            late = [r["id"] for r in result["actions"] if terminal_seq and (r.get("gate_seq") or 0) > terminal_seq]
            if late:
                failures.append(f"dispatch after terminal: {late}")
        elif kind == "trace_contains":
            if not any(row["kind"] == check["kind"] and check["value"] in row["data"] for row in result["trace"]):
                failures.append(f"trace lacks {check['kind']}:{check['value']}")
        elif kind == "proposed":
            if not any(row["kind"] == "proposal" and row["data"] and row["data"][0].get("target_id") == check["target_id"]
                       for row in result["trace"]):
                failures.append(f"{check['target_id']} was never proposed")
        elif kind == "revoked":
            if not any(r["operation"] == check["operation"] for r in result.get("policy", {}).get("revocations", [])):
                failures.append(f"no recorded revocation of {check['operation']}")
        elif kind == "cleanup" and result["cleanup"] not in check["in"]:
            failures.append(f"cleanup {result['cleanup']} not in {check['in']}")
        elif kind == "deadline_delay":
            delay = timing.get("deadline_delay_seconds")
            if delay is None or delay > 0.25:
                failures.append(f"deadline transition delay {delay}")
        elif kind == "exit_bound":
            if timing.get("exit_after_terminal_seconds", 1e9) > GRACE + KILL + 1.0:
                failures.append(f"exit {timing.get('exit_after_terminal_seconds')} s after terminal")
        elif kind == "no_secrets":
            texts = [(directory / name).read_bytes() for name in ("stdout.json", "stderr.log")]
            texts += [p.read_bytes() for p in (directory / "out").glob("*") if p.is_file()]
            for value in check["values"]:
                if any(value.encode() in text for text in texts):
                    failures.append(f"sentinel value leaked into ordinary logs/artifacts")
        elif kind == "evidence_linked":
            evidence = (directory / "out" / "evidence.jsonl").read_text(encoding="utf-8")
            missing = [ref for ref in result.get("evidence_refs", []) if ref not in evidence]
            if missing or not result.get("evidence_refs"):
                failures.append(f"evidence refs not linked: {missing or 'none'}")
        elif kind == "old_export_landed":
            if extra.get("old_exports") != 1 or state.get("state", {}).get("exports") != 0:
                failures.append(f"old run exports {extra.get('old_exports')}, new run exports "
                                f"{state.get('state', {}).get('exports')}")
    return failures


def summary(result):
    if not result:
        return None
    return {key: result.get(key) for key in ("attempt_id", "attempt_status", "stop_reason", "task_outcome",
                                             "execution_outcome", "cleanup", "timing", "faults")} | {
        "budget": result.get("budget"),
        "verification": {r["id"]: r["status"] for r in result.get("verification", [])},
        "actions": [{k: r.get(k) for k in ("id", "operation", "target_id", "slot_ref", "effect", "return_seq")}
                    for r in result.get("actions", [])],
        "routing": {k: (result.get("routing") or {}).get(k) for k in ("status", "route", "provider", "is_stub")}}


def attempt(level: str, manifest: dict, case: dict, rep: int, options, root: Path) -> dict:
    directory = root / level / case["id"] / f"rep-{rep}"
    directory.mkdir(parents=True, exist_ok=False)
    stamp = f"{level.lower()}-{case['id'].lower().replace('.', '-')}-{rep}-{int(time.time() * 1000) % 10**8}"
    faults = copy.deepcopy(case.get("faults") or {})
    extra = {}
    old = None
    if case.get("kind") == "old_delayed_export":
        # Old run: an export committed and then cancelled while the slow server is still working.
        old = Fixture(stamp + "-old", directory, slow_seconds=4.0)
        old_dir = directory / "old"
        old_dir.mkdir()
        old_task = build_task(manifest, case, old, old.run_id)
        extra["old_run"] = summary(run_cli(level, old_dir, old_task, case, manifest, options, faults)["result"])
        faults = {}
        case = {**case, "script": [{"action": "finish"}]}
    fixture = Fixture(stamp, directory)
    try:
        run_id = stamp + "-other" if case.get("fixture_run_id") == "mismatch" else stamp
        task = build_task(manifest, case, fixture, run_id)
        run = run_cli(level, directory, task, case, manifest, options, faults)
        state = fixture.settled_state()
        if old is not None:
            extra["old_exports"] = old.settled_state(wait=8.0).get("state", {}).get("exports")
        failures = evaluate(case, run, state, task, directory, extra)
    finally:
        fixture.stop()
        if old is not None:
            old.stop()
    return {"case": case["id"], "level": level, "repetition": rep, "status": "failed" if failures else "passed",
            "failures": failures, "directory": str(directory.relative_to(root)), "run_id": stamp,
            "exit_code": run["exit_code"], "process_seconds": run["process_seconds"],
            "command": run["command"], "result": summary(run["result"]), **extra}


def environment(options) -> dict:
    def version(name):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    def git(path):
        try:
            return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True,
                                  check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True).stdout.strip()
    chrome = subprocess.run([options.chrome, "--version"], capture_output=True, text=True).stdout.strip()
    import systemone_harness
    harness_dir = Path(systemone_harness.__file__).resolve().parents[1]
    info = {"git_head": git(ROOT), "git_dirty": bool(dirty), "python": platform.python_version(),
            "platform": platform.platform(), "harness_commit": git(harness_dir),
            "systemone_harness_version": version("systemone-harness"), "browser_use_version": version("browser-use"),
            "chromium": chrome, "user": os.environ.get("USER")}
    if options.level == "J":
        info["jev_url"] = options.jev_url
        info["micro_model"] = os.environ.get("S1_MODEL", "~typesafe/jev-latest")
        info["micro_provider"] = "openrouter" if os.environ.get("OPENROUTER_API_KEY") else (
            "typesafe" if os.environ.get("TYPESAFE_API_KEY") else None)
        info["https_proxy_configured"] = bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
        try:
            with NO_PROXY.open(options.jev_url + "/health", timeout=5) as response:
                info["jevrouter_health"] = json.load(response)
        except (OSError, ValueError) as exc:
            info["jevrouter_health"] = f"unavailable: {type(exc).__name__}"
        info["jevrouter_commit"] = options.jevrouter_commit
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--level", choices=("B", "J"), required=True)
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--output", required=True, type=Path, help="new JSON results file")
    parser.add_argument("--artifacts", type=Path, help="attempt directories (default: next to output)")
    parser.add_argument("--jev-url", default="http://127.0.0.1:8797")
    parser.add_argument("--jevrouter-commit")
    parser.add_argument("--only", action="append", default=[], help="run only these case IDs (exploratory)")
    parser.add_argument("--repetitions", type=int, help="override (exploratory runs only)")
    options = parser.parse_args()
    manifest_path = Path(__file__).with_name("bj-manifest.json")
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema_version") != "v05-bj-manifest/0.1":
        raise ValueError("unsupported manifest")
    cases = [c for c in manifest["cases"] if options.level in c["levels"] and (not options.only or c["id"] in options.only)]
    repetitions = options.repetitions or manifest["repetitions"]
    root = options.artifacts or options.output.with_suffix("")
    root.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": "v05-bj-results/0.1", "level": options.level,
              "manifest_sha256": hashlib.sha256(raw).hexdigest(), "exploratory": bool(options.only or options.repetitions),
              "repetitions": repetitions, "environment": environment(options),
              "command": [Path(sys.argv[0]).name, *sys.argv[1:]], "started_at_unix": time.time(),
              "artifacts": str(root), "attempts": [{"case": c["id"], "repetition": r, "status": "pending"}
                                                   for c in cases for r in range(1, repetitions + 1)]}
    options.output.parent.mkdir(parents=True, exist_ok=True)
    with options.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=1)
    for index, row in enumerate(report["attempts"]):
        case = next(c for c in cases if c["id"] == row["case"])
        try:
            report["attempts"][index] = attempt(options.level, manifest, case, row["repetition"], options, root)
        except Exception as exc:  # noqa: BLE001 - the attempt is recorded as failed, never dropped
            report["attempts"][index] = {**row, "level": options.level, "status": "failed",
                                         "failures": [f"runner error: {type(exc).__name__}: {exc}"]}
        done = report["attempts"][index]
        print(json.dumps({"case": done["case"], "rep": done["repetition"], "status": done["status"],
                          "failures": done.get("failures")}), flush=True)
        checkpoint = options.output.with_suffix(options.output.suffix + ".tmp")
        checkpoint.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        checkpoint.replace(options.output)
    report["counts"] = {key: sum(a["status"] == key for a in report["attempts"]) for key in ("passed", "failed", "pending")}
    report["finished_at_unix"] = time.time()
    options.output.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(report["counts"]))
    return 1 if report["counts"]["failed"] or report["counts"]["pending"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
