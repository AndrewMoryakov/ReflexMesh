"""V0.6 B gate through the MCP Task API: real Chromium, a fresh fixture per repetition.

    python experiments/v06/run_mcp_b.py --chrome /path/to/chrome --output results.json

Cases (docs/specs/V0.6.md section 9): A06 form via MCP, A07 support request with a content gap,
handoff and continuation, A08 cancellation during a held decision. Every planned repetition is
preserved; the output file must not exist yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "experiments/v05")]
from run_bj import Fixture, environment  # noqa: E402

SLOTS = [{"id": "name", "version": 1, "value": "Test User"},
         {"id": "email", "version": 1, "value": "test@example.com"}]
MESSAGE = {"id": "message", "version": 1, "value": "Please call me back about my order."}
LIMITS = {"wall_seconds": 120, "max_steps": 24, "max_model_calls": 32, "max_action_retries": 0}
FORM_SCRIPT = [{"action": "type_text", "target_id": "name", "slot": "name@1"},
               {"action": "type_text", "target_id": "email", "slot": "email@1"},
               {"action": "click", "target_id": "send-form"}, {"action": "finish"}]
SUPPORT_FIRST = [{"action": "type_text", "target_id": "support-name", "slot": "name@1"},
                 {"action": "type_text", "target_id": "support-email", "slot": "email@1"},
                 {"action": "click", "target_id": "send-support"}, {"action": "finish"}]
SUPPORT_SECOND = [{"action": "type_text", "target_id": "support-name", "slot": "name@1"},
                  {"action": "type_text", "target_id": "support-email", "slot": "email@1"},
                  {"action": "type_text", "target_id": "support-message", "slot": "message@1"},
                  {"action": "click", "target_id": "send-support"}, {"action": "finish"}]
CASES = ["A06.form_via_mcp", "A07.needs_content_then_continue", "A08.cancel_held_decision"]


class Client:
    def __init__(self, command, env, log):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                        env=env, text=True, bufsize=1)
        self.lock, self.pending, self.next_id = threading.Lock(), {}, 0
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "run_mcp_b", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self):
        for line in self.process.stdout:
            message = json.loads(line)
            with self.lock:
                self.pending[message.get("id")] = message

    def _send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method, params, timeout=120):
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            with self.lock:
                if request_id in self.pending:
                    return self.pending.pop(request_id)
            time.sleep(0.02)
        raise TimeoutError(method)

    def tool(self, name, **arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})["result"]
        return result["structuredContent"], result["isError"]

    def wait(self, attempt_id, limit=400):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            status, error = self.tool("reflexmesh_status", attempt_id=attempt_id, wait_seconds=50)
            if error or status["state"] != "running":
                return status
        raise TimeoutError(attempt_id)

    def close(self):
        self.process.stdin.close()
        return self.process.wait(timeout=30)


def task(fixture, run_id, case, **overrides):
    base = {"schema_version": "execution-task/0.1", "task_id": case.lower().replace(".", "-"), "revision": 1,
            "allowed_executors": ["browser.soh"], "fixture": {"origin": fixture.origin, "run_id": run_id},
            "limits": dict(LIMITS)}
    base.update(overrides)
    return base


def posts(state, path):
    return [e for e in state.get("log", []) if e.get("method") == "POST" and e.get("path") == path]


def run_case(case, client, directory, level, providers):
    stamp = f"{case.lower().replace('.', '-')}-{int(time.time() * 1000) % 10**8}"
    fixture = Fixture(stamp, directory)
    failures, record = [], {}
    scripted = providers["action_provider"] == "script"
    try:
        if case == "A06.form_via_mcp":
            spec = task(fixture, stamp, case, goal="Fill in the contact form with the supplied name and email, "
                        "then send it once.", start_path="/form",
                        permissions=["navigate", "type_text", "submit_form"], text_slots=SLOTS,
                        criteria=[{"id": "sent", "kind": "postcondition", "predicate": "form_submitted_once",
                                   "args": {"name_slot": "name@1", "email_slot": "email@1"}}])
            submitted, error = client.tool("reflexmesh_submit", task=spec, **providers,
                                           **({"script": FORM_SCRIPT} if scripted else {}))
            if error:
                return [f"submit error {submitted}"], record
            done = client.wait(submitted["attempt_id"])
            result = done.get("result") or {}
            record["attempts"] = [summary(done)]
            if (result.get("attempt_status"), result.get("task_outcome")) != ("completed", "pass"):
                failures.append(f"result {result.get('attempt_status')}/{result.get('stop_reason')}")
            if result.get("handoff") is not None:
                failures.append("completed attempt carried a handoff")
            if len(posts(fixture.settled_state(), "/form")) != 1:
                failures.append("POST /form count != 1")
        elif case == "A07.needs_content_then_continue":
            spec = task(fixture, stamp, case, goal="Send a support request with the supplied name and email.",
                        start_path="/support", permissions=["navigate", "type_text", "submit_form"],
                        text_slots=SLOTS,
                        criteria=[{"id": "sent", "kind": "postcondition", "predicate": "support_request_sent_once",
                                   "args": {"name_slot": "name@1", "email_slot": "email@1"}}])
            first, error = client.tool("reflexmesh_submit", task=spec, **providers,
                                       **({"script": SUPPORT_FIRST} if scripted else {}))
            if error:
                return [f"submit error {first}"], record
            done = client.wait(first["attempt_id"])
            result = done.get("result") or {}
            handoff = result.get("handoff") or {}
            record["attempts"] = [summary(done)]
            if handoff.get("kind") != "needs_content":
                failures.append(f"first attempt handoff kind {handoff.get('kind')!r} "
                                f"({result.get('attempt_status')}/{result.get('stop_reason')})")
            if [n.get("target_id") for n in handoff.get("needs", [])] != ["support-message"]:
                failures.append(f"needs {handoff.get('needs')}")
            if not (handoff.get("continuation") or {}).get("allowed"):
                failures.append(f"continuation {handoff.get('continuation')}")
            if any(a.get("outcome") == "possibly_done" for a in handoff.get("actions", [])):
                failures.append("first attempt left a possibly_done action")
            accepted_before = [e for e in posts(fixture.settled_state(), "/support") if not e.get("rejected")]
            if accepted_before:
                failures.append("a support request was accepted without a message")
            second, error = client.tool("reflexmesh_continue", parent_attempt_id=first["attempt_id"],
                                        changes={"add_text_slots": [MESSAGE]}, **providers,
                                        **({"script": SUPPORT_SECOND} if scripted else {}))
            if error:
                return failures + [f"continue error {second}"], record
            done2 = client.wait(second["attempt_id"])
            result2 = done2.get("result") or {}
            record["attempts"].append(summary(done2))
            record["continuation_limits"] = second.get("limits")
            if (result2.get("attempt_status"), result2.get("task_outcome")) != ("completed", "pass"):
                failures.append(f"continuation result {result2.get('attempt_status')}/{result2.get('stop_reason')}")
            chain = result2.get("chain") or {}
            if (chain.get("parent_attempt_id"), chain.get("root_attempt_id"), chain.get("sequence")) != (
                    first["attempt_id"], first["attempt_id"], 2):
                failures.append(f"chain {chain}")
            if second.get("revision") != 2:
                failures.append(f"continuation revision {second.get('revision')}")
            limits = second.get("limits") or {}
            if not (limits.get("max_steps", 99) < LIMITS["max_steps"] and
                    limits.get("wall_seconds", 999) < LIMITS["wall_seconds"]):
                failures.append(f"chain budget did not decrease: {limits}")
            state = fixture.settled_state()
            accepted = [e for e in posts(state, "/support") if not e.get("rejected")]
            if len(accepted) != 1 or (accepted[0].get("data") or {}).get("message") != MESSAGE["value"]:
                failures.append("expected exactly one accepted support request with the prepared message")
        elif case == "A08.cancel_held_decision":
            spec = task(fixture, stamp, case, goal="Fill in the contact form with the supplied name and email, "
                        "then send it once.", start_path="/form",
                        permissions=["navigate", "type_text", "submit_form"], text_slots=SLOTS,
                        criteria=[{"id": "sent", "kind": "postcondition", "predicate": "form_submitted_once",
                                   "args": {"name_slot": "name@1", "email_slot": "email@1"}}])
            submitted, error = client.tool("reflexmesh_submit", task=spec, **providers,
                                           faults={"hold_decision": {"at": 2, "seconds": 600}},
                                           **({"script": FORM_SCRIPT} if scripted else {}))
            if error:
                return [f"submit error {submitted}"], record
            attempt_dir = Path(client.state_dir) / "attempts" / submitted["attempt_id"]
            # Wait until the held (second) decision is pending; the barrier announces it.
            limit = time.monotonic() + 100
            while time.monotonic() < limit:
                state, _ = client.tool("reflexmesh_status", attempt_id=submitted["attempt_id"])
                if state["state"] != "running" or (attempt_dir / "fault-hold-started").is_file():
                    break
                time.sleep(0.2)
            record["hold_observed"] = (attempt_dir / "fault-hold-started").is_file()
            if not record["hold_observed"]:
                failures.append("held decision was never reached")
            cancel, _ = client.tool("reflexmesh_cancel", attempt_id=submitted["attempt_id"])
            done = client.wait(submitted["attempt_id"])
            result = done.get("result") or {}
            record["attempts"] = [summary(done)]
            record["cancel"] = cancel
            if (result.get("attempt_status"), result.get("stop_reason")) != ("cancelled", "cancel_requested"):
                failures.append(f"result {result.get('attempt_status')}/{result.get('stop_reason')}")
            if any(a.get("target_id") == "send-form" for a in result.get("actions", [])):
                failures.append("send-form dispatched after cancellation")
            if (result.get("handoff") or {}).get("kind") != "cancelled":
                failures.append(f"handoff {(result.get('handoff') or {}).get('kind')}")
            if posts(fixture.settled_state(), "/form"):
                failures.append("form was submitted")
    finally:
        fixture.stop()
    return failures, record


def summary(status):
    result = status.get("result") or {}
    return {"attempt_id": status.get("attempt_id"), "state": status.get("state"),
            **{k: result.get(k) for k in ("attempt_status", "stop_reason", "task_outcome", "revision", "chain",
                                          "budget", "cleanup")},
            "handoff": {k: (result.get("handoff") or {}).get(k) for k in ("kind", "needs", "continuation",
                                                                         "remaining_budget", "actions")},
            "actions": result.get("actions")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--level", choices=("B", "J"), default="B")
    parser.add_argument("--jev-url", default="http://127.0.0.1:8797")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--only", action="append", default=[])
    options = parser.parse_args()
    root = options.output.with_suffix("")
    root.mkdir(parents=True, exist_ok=False)
    providers = ({"routing_provider": "stub", "action_provider": "script"} if options.level == "B" else
                 {"routing_provider": "jevrouter", "action_provider": "jev"})
    cases = [c for c in CASES if not options.only or c in options.only]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")]))}
    report = {"schema_version": "v06-mcp-results/0.1", "level": options.level, "providers": providers,
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "environment": environment(argparse.Namespace(chrome=options.chrome, level=options.level,
                                                            jev_url=options.jev_url, jevrouter_commit=None)),
              "repetitions": options.repetitions, "started_at_unix": time.time(),
              "attempts": [{"case": c, "repetition": r, "status": "pending"}
                           for c in cases for r in range(1, options.repetitions + 1)]}
    with options.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=1)
    for index, row in enumerate(report["attempts"]):
        directory = root / row["case"] / f"rep-{row['repetition']}"
        directory.mkdir(parents=True)
        state_dir = directory / "state"
        command = [sys.executable, "-m", "reflexmesh", "mcp", "--state-dir", str(state_dir), "--chrome",
                   options.chrome, "--jev-url", options.jev_url, "--allow-faults"]
        with open(directory / "mcp-server.log", "w") as log:
            client = Client(command, env, log)
            client.state_dir = state_dir
            started = time.monotonic()
            try:
                failures, record = run_case(row["case"], client, directory, options.level, providers)
            except Exception as exc:  # noqa: BLE001 - recorded, never dropped
                failures, record = [f"runner error: {type(exc).__name__}: {exc}"], {}
            finally:
                code = client.close()
        report["attempts"][index] = {**row, "status": "failed" if failures else "passed", "failures": failures,
                                     "seconds": round(time.monotonic() - started, 3), "server_exit": code,
                                     "directory": str(directory.relative_to(root)), **record}
        print(json.dumps({"case": row["case"], "rep": row["repetition"], "status": report["attempts"][index]["status"],
                          "failures": failures}), flush=True)
        options.output.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    report["counts"] = {k: sum(a["status"] == k for a in report["attempts"]) for k in ("passed", "failed", "pending")}
    report["finished_at_unix"] = time.time()
    options.output.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(report["counts"]))
    return 1 if report["counts"]["failed"] or report["counts"]["pending"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
