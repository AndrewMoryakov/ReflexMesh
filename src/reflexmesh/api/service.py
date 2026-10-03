"""Transport-neutral Task API (V0.6): submit, status, cancel, continue.

Each attempt runs as `python -m reflexmesh run` in its own child process, so a task submitted
through MCP or any other transport has exactly the semantics of the CLI (INV-01): the same
supervisor, deadline, SIGINT cancellation, evidence and result files. The service prepares
inputs, starts and observes the process, and links continuation attempts into chains.
"""

from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import reflexmesh
from reflexmesh.adapters.system_one.script_selector import validate_script
from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.contracts.task import ValidationError
from reflexmesh.runtime.handoff import remaining_budget
from reflexmesh.runtime.runner import GRACE_SECONDS, KILL_SECONDS

ROUTING_PROVIDERS = ("stub", "jevrouter")
ACTION_PROVIDERS = ("script", "jev")
CHANGE_KEYS = {"goal", "add_text_slots", "criteria", "start_path", "fixture", "permissions"}
MAX_WAIT_SECONDS = 50.0
SHUTDOWN_BOUND = GRACE_SECONDS + KILL_SECONDS + 1.0


class ApiError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code, self.message = code, message or code

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class TaskService:
    def __init__(self, state_dir, *, chrome: str | None = None, jev_url: str = "http://127.0.0.1:8787",
                 max_concurrent: int = 1, python: str | None = None, env: dict | None = None,
                 allow_faults: bool = False):
        if type(max_concurrent) is not int or max_concurrent < 1:
            raise ValueError("max_concurrent must be a positive integer")
        self.root = Path(state_dir).resolve()
        (self.root / "attempts").mkdir(parents=True, exist_ok=True)
        self.chrome, self.jev_url, self.max_concurrent = chrome, jev_url, max_concurrent
        self.allow_faults = allow_faults  # Acceptance barriers only; recorded in every result.
        self.python = python or sys.executable
        package_root = str(Path(reflexmesh.__file__).resolve().parents[1])
        base = dict(os.environ if env is None else env)
        base["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, base.get("PYTHONPATH")]))
        self.env = base
        self.lock = threading.Lock()
        self.processes: dict[str, subprocess.Popen] = {}
        self.closed = False

    # ── helpers ──
    def _dir(self, attempt_id: str) -> Path:
        if type(attempt_id) is not str or len(attempt_id) != 32 or any(c not in "0123456789abcdef" for c in attempt_id):
            raise ApiError("unknown_attempt", "attempt IDs are 32 lowercase hex characters")
        directory = self.root / "attempts" / attempt_id
        if not (directory / "request.json").is_file():
            raise ApiError("unknown_attempt", f"no attempt {attempt_id}")
        return directory

    @staticmethod
    def _read(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    def _validate(self, task: dict, routing_provider, action_provider, script) -> ExecutionTask:
        if routing_provider not in ROUTING_PROVIDERS or action_provider not in ACTION_PROVIDERS:
            raise ApiError("invalid_input", "routing_provider must be stub|jevrouter and action_provider script|jev")
        if (action_provider == "script") != (script is not None):
            raise ApiError("invalid_input", "a script is required with, and only with, the script action provider")
        try:
            parsed = ExecutionTask.from_dict(copy.deepcopy(task))
            if script is not None:
                validate_script(copy.deepcopy(script))
        except (ValidationError, ValueError, TypeError) as exc:
            raise ApiError("invalid_input", str(exc)) from None
        if len(json.dumps(task, ensure_ascii=True).encode()) > 65536:
            raise ApiError("invalid_input", "task exceeds 64 KiB")
        return parsed

    def _running(self) -> int:
        return sum(1 for process in self.processes.values() if process.poll() is None)

    def _faults(self, faults):
        if faults is None:
            return None
        if not self.allow_faults:
            raise ApiError("invalid_input", "fault injection is disabled on this server")
        from reflexmesh.runtime.faults import validate_faults
        try:
            return validate_faults(faults)
        except ValidationError as exc:
            raise ApiError("invalid_input", str(exc)) from None

    def _start(self, task: dict, routing_provider: str, action_provider: str, script, chain: dict | None,
               faults: dict | None = None) -> dict:
        with self.lock:
            if self.closed:
                raise ApiError("unavailable", "the service is shutting down")
            if self._running() >= self.max_concurrent:
                raise ApiError("busy", f"at most {self.max_concurrent} attempt(s) may run at once")
            attempt_id = uuid.uuid4().hex
            directory = self.root / "attempts" / attempt_id
            directory.mkdir(parents=True)
            (directory / "task.json").write_text(json.dumps(task, indent=1), encoding="utf-8")
            command = [self.python, "-m", "reflexmesh", "run", "--input", "task.json", "--output-dir", "out",
                       "--routing-provider", routing_provider, "--action-provider", action_provider,
                       "--attempt-id", attempt_id]
            if script is not None:
                (directory / "script.json").write_text(json.dumps(script, indent=1), encoding="utf-8")
                command += ["--script", "script.json"]
            if self.chrome:
                command += ["--chrome", self.chrome]
            if routing_provider == "jevrouter":
                command += ["--jev-url", self.jev_url]
            if chain is not None:
                (directory / "chain.json").write_text(json.dumps(chain, indent=1), encoding="utf-8")
                command += ["--chain", "chain.json"]
            if faults:
                (directory / "faults.json").write_text(json.dumps(faults, indent=1), encoding="utf-8")
                command += ["--faults", "faults.json"]
            request = {"attempt_id": attempt_id, "submitted_at": time.time(), "routing_provider": routing_provider,
                       "action_provider": action_provider, "scripted": script is not None,
                       "parent_attempt_id": chain and chain["parent_attempt_id"]}
            (directory / "request.json").write_text(json.dumps(request, indent=1), encoding="utf-8")
            stdout = open(directory / "stdout.json", "wb")
            stderr = open(directory / "stderr.log", "wb")
            try:
                # Own session: a client killing the server's process group does not bypass the
                # attempt's graceful cancellation; shutdown() reaps attempts explicitly.
                self.processes[attempt_id] = subprocess.Popen(command, cwd=directory, env=self.env,
                                                              stdin=subprocess.DEVNULL, stdout=stdout,
                                                              stderr=stderr, start_new_session=True)
            finally:
                stdout.close()
                stderr.close()
        return {"attempt_id": attempt_id, "state": "running", "parent_attempt_id": request["parent_attempt_id"]}

    def _result(self, directory: Path) -> dict | None:
        path = directory / "out" / "result.json"
        if path.is_file():
            return self._read(path)
        raw = (directory / "stdout.json").read_text(encoding="utf-8") if (directory / "stdout.json").is_file() else ""
        return json.loads(raw) if raw.strip() else None

    # ── API ──
    def submit(self, task: dict, routing_provider: str, action_provider: str, script=None, faults=None) -> dict:
        self._validate(task, routing_provider, action_provider, script)
        return self._start(task, routing_provider, action_provider, script, None, self._faults(faults))

    def status(self, attempt_id: str, wait_seconds: float = 0.0, detail: str = "summary") -> dict:
        directory = self._dir(attempt_id)
        if detail not in ("summary", "full"):
            raise ApiError("invalid_input", "detail must be summary or full")
        if type(wait_seconds) not in (int, float) or not 0 <= wait_seconds <= MAX_WAIT_SECONDS:
            raise ApiError("invalid_input", f"wait_seconds must be within 0..{MAX_WAIT_SECONDS:g}")
        limit = time.monotonic() + wait_seconds
        process = self.processes.get(attempt_id)
        while process is not None and process.poll() is None and time.monotonic() < limit:
            time.sleep(0.1)
        request = self._read(directory / "request.json")
        if process is not None and process.poll() is None:
            return {"attempt_id": attempt_id, "state": "running",
                    "elapsed_seconds": round(time.time() - request["submitted_at"], 3),
                    "parent_attempt_id": request.get("parent_attempt_id")}
        result = self._result(directory)
        if result is None:
            error = (directory / "stderr.log").read_text(encoding="utf-8", errors="replace")[-2000:]
            code = "invalid_input" if '"invalid_input"' in error else (
                "output_unavailable" if '"output_unavailable"' in error else "attempt_lost")
            return {"attempt_id": attempt_id, "state": "rejected" if process is not None else "lost",
                    "error": {"code": code}}
        if detail == "full":
            return {"attempt_id": attempt_id, "state": "terminal", "result": result}
        return {"attempt_id": attempt_id, "state": "terminal", "exit_code": process.returncode if process else None,
                "result": summarize(result)}

    def cancel(self, attempt_id: str) -> dict:
        self._dir(attempt_id)
        process = self.processes.get(attempt_id)
        if process is None or process.poll() is not None:
            return {"attempt_id": attempt_id, "cancel": "already_terminal"}
        try:
            process.send_signal(signal.SIGINT)  # The CLI's own cancellation path (same as Ctrl-C).
        except ProcessLookupError:
            return {"attempt_id": attempt_id, "cancel": "already_terminal"}
        return {"attempt_id": attempt_id, "cancel": "requested",
                "note": "acceptance is decided at the attempt's gate; check status for the terminal state"}

    def continue_task(self, parent_attempt_id: str, changes: dict, routing_provider: str, action_provider: str,
                      script=None, faults=None) -> dict:
        directory = self._dir(parent_attempt_id)
        faults = self._faults(faults)
        process = self.processes.get(parent_attempt_id)
        if process is not None and process.poll() is None:
            raise ApiError("not_terminal", "the parent attempt is still running")
        parent = self._result(directory)
        if parent is None:
            raise ApiError("continuation_refused", "the parent attempt has no result")
        if parent.get("attempt_status") == "completed":
            raise ApiError("continuation_refused", "the parent completed; submit the next subtask as a new task")
        handoff = parent.get("handoff") or {}
        allowed = (handoff.get("continuation") or {})
        if not allowed.get("allowed"):
            reason = allowed.get("reason") or "continuation_refused"
            raise ApiError(reason if reason in ("unresolved_effect", "budget_exhausted") else "continuation_refused",
                           f"continuation not allowed: {reason}")
        if type(changes) is not dict or set(changes) - CHANGE_KEYS:
            raise ApiError("invalid_input", f"changes may contain only {sorted(CHANGE_KEYS)}")
        task = self._read(directory / "task.json")
        new = copy.deepcopy(task)
        if "goal" in changes:
            new["goal"] = changes["goal"]
        if "criteria" in changes:
            new["criteria"] = changes["criteria"]
        if "start_path" in changes:
            new["start_path"] = changes["start_path"]
        if "fixture" in changes:
            new["fixture"] = changes["fixture"]
        if "add_text_slots" in changes:
            added = changes["add_text_slots"]
            if type(added) is not list or not added:
                raise ApiError("invalid_input", "add_text_slots must be a nonempty list")
            existing = {(s["id"], s["version"]) for s in task["text_slots"]}
            if any(type(s) is not dict or (s.get("id"), s.get("version")) in existing for s in added):
                raise ApiError("invalid_input", "existing slot versions are immutable; declare a new version")
            new["text_slots"] = task["text_slots"] + added
        if "permissions" in changes:
            permissions = changes["permissions"]
            if type(permissions) is not list or not set(permissions) <= set(task["permissions"]):
                raise ApiError("invalid_input", "a continuation can only narrow permissions")
            new["permissions"] = permissions
        if new != task:
            new["revision"] = task["revision"] + 1
        parent_chain = parent.get("chain") or {}
        root_id = parent_chain.get("root_attempt_id") or parent_attempt_id
        root_task = self._read(self._dir(root_id) / "task.json")
        root_limits = dict(root_task["limits"])
        used = parent_chain.get("used") or {"steps": 0, "model_calls": 0, "wall_seconds": 0.0}
        left = remaining_budget(root_limits, used)
        if left["steps"] < 1 or left["model_calls"] < 1 or left["wall_seconds"] < 1:
            raise ApiError("budget_exhausted", f"remaining chain budget {left}")
        new["limits"] = {"wall_seconds": left["wall_seconds"], "max_steps": left["steps"],
                         "max_model_calls": left["model_calls"], "max_action_retries": 0}
        self._validate(new, routing_provider, action_provider, script)
        chain = {"root_attempt_id": root_id, "parent_attempt_id": parent_attempt_id,
                 "sequence": int(parent_chain.get("sequence") or 1) + 1, "root_limits": root_limits,
                 "used": {"steps": int(used.get("steps", 0)), "model_calls": int(used.get("model_calls", 0)),
                          "wall_seconds": float(used.get("wall_seconds", 0.0))}}
        started = self._start(new, routing_provider, action_provider, script, chain, faults)
        return {**started, "revision": new["revision"], "limits": new["limits"], "chain_sequence": chain["sequence"]}

    def shutdown(self) -> dict:
        """Cancel running attempts through their own path, then reap them within a bound."""
        with self.lock:
            self.closed = True
            running = {k: p for k, p in self.processes.items() if p.poll() is None}
        for process in running.values():
            try:
                process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
        limit = time.monotonic() + SHUTDOWN_BOUND + 2.0
        while time.monotonic() < limit and any(p.poll() is None for p in running.values()):
            time.sleep(0.05)
        forced = []
        for attempt_id, process in running.items():
            if process.poll() is None:
                forced.append(attempt_id)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                process.wait(timeout=2)
        return {"cancelled": sorted(running), "forced": forced}


def summarize(result: dict) -> dict:
    """Decision-relevant fields; the full result (with trace) stays in the attempt directory."""
    return {
        **{key: result.get(key) for key in ("schema_version", "task_id", "revision", "attempt_id", "executor_id",
                                            "attempt_status", "stop_reason", "task_outcome", "execution_outcome",
                                            "cleanup", "chain", "handoff", "evidence_refs")},
        "routing": {k: (result.get("routing") or {}).get(k) for k in ("status", "route", "provider", "is_stub")},
        "verification": [{k: r.get(k) for k in ("id", "kind", "status")} for r in result.get("verification", [])],
        "actions": [{k: r.get(k) for k in ("id", "operation", "target_id", "slot_ref", "effect")}
                    for r in result.get("actions", [])],
        "budget": result.get("budget"),
    }
