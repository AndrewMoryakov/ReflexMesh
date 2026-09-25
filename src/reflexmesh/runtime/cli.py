"""Execution CLI: validate input, verify fixture identity, then supervise one attempt."""

from __future__ import annotations

import functools
import importlib.util
import json
import signal
import shutil
import sys
import uuid
from pathlib import Path

from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser
from reflexmesh.adapters.system_one.harness import HarnessStrategy
from reflexmesh.adapters.system_one.script_selector import FixtureScriptProvider, validate_script
from reflexmesh.contracts.execution import ExecutionTask, PREDICATES
from reflexmesh.contracts.task import Route, Task, ValidationError
from reflexmesh.routing.stub import route_task
from reflexmesh.routing.jev_router import route_task as jev_route
from reflexmesh.runtime.runner import AttemptSupervisor
from reflexmesh.verification.verifier import FixtureVerifier, read_fixture

EXIT_CODES = {"completed": 0, "blocked": 3, "incomplete": 4, "failed": 5, "cancelled": 130}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValidationError("nonfinite JSON number")


def _load(path):
    if path == "-":
        raw = sys.stdin.buffer.read(65537)
    else:
        with open(path, "rb") as stream:
            raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValidationError("input exceeds 64 KiB")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                      parse_constant=_reject_constant)


def _error(code):
    print(json.dumps({"error": {"code": code}}, ensure_ascii=True), file=sys.stderr)
    return 2


def _blocked(task, reason, routing=None, status="blocked"):
    return {"schema_version": "execution-result/0.1", "task_id": task.task_id,
            "revision": task.revision, "attempt_id": uuid.uuid4().hex, "executor_id": None,
            "routing": routing, "attempt_status": status, "stop_reason": reason,
            "execution_outcome": "not_started", "task_outcome": "unknown",
            "verification": [{"id": c.id, "status": "unknown", "evidence_refs": []} for c in task.criteria],
            "actions": [], "cleanup": "closed", "budget": {"steps": 0, "model_calls": 0,
            "dispatches": 0, "elapsed_seconds": 0, "cost": None, "cost_reason": "not_measured"},
            "trace_ref": "trace.jsonl", "evidence_refs": []}


def _jev_action_provider():
    from systemone_harness.provider import provider_from_env
    return provider_from_env()


def _script_provider(entries):
    return FixtureScriptProvider(entries)


def run_execution(args) -> int:
    try:
        task = ExecutionTask.from_dict(_load(args.input))
        if args.action_provider == "script":
            if not args.script:
                raise ValidationError("script provider requires --script")
            entries = validate_script(_load(args.script))
        elif args.script:
            raise ValidationError("--script requires script action provider")
        else:
            entries = None
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
        return _error("invalid_input")
    output = Path(args.output_dir)
    try:
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            return _error("output_unavailable")
        (output / "trace.jsonl").touch(exist_ok=False)
    except OSError:
        return _error("output_unavailable")

    if any(c.predicate not in PREDICATES for c in task.criteria):
        result = _blocked(task, "unsupported_criterion")
    elif "browser.soh" not in task.allowed_executors:
        result = _blocked(task, "executor_unavailable")
    else:
        try:
            baseline = read_fixture(task, timeout=min(2, task.limits.wall_seconds))
        except ValueError:
            result = _blocked(task, "fixture_mismatch")
        except (OSError, json.JSONDecodeError):
            result = _blocked(task, "executor_unavailable")
        else:
            routing_task = Task("0.1", task.task_id, task.goal, (Route.CUA,), (Route.CUA,))
            routing = (route_task(routing_task).to_dict() if args.routing_provider == "stub"
                       else jev_route(routing_task, endpoint=args.jev_url,
                                      timeout=min(args.timeout, task.limits.wall_seconds)))
            if routing["status"] != "selected" or routing["route"] != "CUA":
                result = _blocked(task, "no_route" if routing["status"] == "abstained"
                                  else "executor_error", routing,
                                  status="failed" if routing["status"] == "failed" else "blocked")
            elif importlib.util.find_spec("systemone_harness") is None or importlib.util.find_spec("browser_use") is None:
                result = _blocked(task, "executor_unavailable", routing)
            else:
                from systemone_harness.envs.browser import default_chrome
                chrome = args.chrome or default_chrome()
                if not chrome or not (Path(chrome).is_file() or shutil.which(chrome)):
                    result = _blocked(task, "executor_unavailable", routing)
                else:
                    strategy = HarnessStrategy(task.goal, functools.partial(FixtureBrowser, task, chrome=chrome),
                                               FixtureBrowser.action_space,
                                               functools.partial(_script_provider, entries) if entries is not None
                                               else _jev_action_provider,
                                               admit=None, model=entries is None, bootstrap=True)
                    supervisor = AttemptSupervisor(task, strategy, FixtureVerifier(task, baseline))
                    previous = signal.getsignal(signal.SIGINT)
                    signal.signal(signal.SIGINT, lambda *_: supervisor.cancel())
                    try:
                        result = supervisor.run()
                    finally:
                        signal.signal(signal.SIGINT, previous)
                    result.update(executor_id="browser.soh", routing=routing, trace_ref="trace.jsonl",
                                  evidence_refs=[ref for row in result["verification"]
                                                 for ref in row.get("evidence_refs", [])])
    try:
        (output / "trace.jsonl").write_text("".join(json.dumps({"event": row}, ensure_ascii=True) + "\n"
                                                 for row in result.get("trace", result["actions"])), encoding="utf-8")
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    except OSError:
        return _error("output_unavailable")
    print(json.dumps(result, ensure_ascii=True))
    return EXIT_CODES[result["attempt_status"]]
