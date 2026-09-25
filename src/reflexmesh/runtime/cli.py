"""Execution CLI: validate input, verify fixture identity, then supervise one attempt."""

from __future__ import annotations

import functools
import importlib.util
import json
import signal
import shutil
import sys
from pathlib import Path

from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser
from reflexmesh.adapters.system_one.harness import HarnessStrategy
from reflexmesh.adapters.system_one.script_selector import FixtureScriptProvider, validate_script
from reflexmesh.contracts.execution import ExecutionTask, PREDICATES
from reflexmesh.contracts.task import Route, Task, ValidationError
from reflexmesh.routing.stub import route_task
from reflexmesh.routing.jev_router import route_task as jev_route, validate_config
from reflexmesh.runtime.runner import AttemptSupervisor, WorkerResult
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


def _jev_action_provider():
    from systemone_harness.provider import provider_from_env
    provider = provider_from_env()
    provider.retries = 0  # No opaque HTTP retries outside the model-call budget.
    return provider


def _script_provider(entries):
    return FixtureScriptProvider(entries)


class BrowserExecutionStrategy:
    """Run preflight, macro routing and the browser loop in one supervised worker."""

    def __init__(self, task, args, entries):
        self.task, self.args, self.entries = task, args, entries

    def __call__(self, gate, events):
        task, args = self.task, self.args
        gate.remaining()
        if any(c.predicate not in PREDICATES for c in task.criteria):
            return WorkerResult("blocked", "unsupported_criterion")
        if "browser.soh" not in task.allowed_executors:
            return WorkerResult("blocked", "executor_unavailable")
        try:
            baseline = read_fixture(task, timeout=min(2.0, gate.remaining()))
        except ValueError:
            return WorkerResult("blocked", "fixture_mismatch")
        except (OSError, json.JSONDecodeError):
            return WorkerResult("blocked", "executor_unavailable")
        events.put(("baseline", baseline))  # Private IPC; never written to the trace.

        routing_task = Task("0.1", task.task_id, task.goal, (Route.CUA,), (Route.CUA,))
        if args.routing_provider == "jevrouter":
            gate.reserve_call()
            routing = jev_route(routing_task, endpoint=args.jev_url,
                                timeout=min(args.timeout, gate.remaining()))
        else:
            gate.remaining()
            routing = route_task(routing_task).to_dict()
        events.put(("routing", routing))
        if routing["status"] == "needs_confirmation":
            return WorkerResult("blocked", "policy_denied")
        if routing["status"] == "failed":
            return WorkerResult("executor_error", "routing_error")
        if routing["status"] != "selected" or routing["route"] != "CUA":
            return WorkerResult("blocked", "no_route")

        if importlib.util.find_spec("systemone_harness") is None or importlib.util.find_spec("browser_use") is None:
            return WorkerResult("blocked", "executor_unavailable")
        from systemone_harness.envs.browser import default_chrome
        chrome = args.chrome or default_chrome()
        if not chrome or not (Path(chrome).is_file() or shutil.which(chrome)):
            return WorkerResult("blocked", "executor_unavailable")
        gate.remaining()
        events.put(("executor_selected", "browser.soh"))
        strategy = HarnessStrategy(task.goal, functools.partial(FixtureBrowser, task, chrome=chrome),
                                   FixtureBrowser.action_space,
                                   functools.partial(_script_provider, self.entries) if self.entries is not None
                                   else _jev_action_provider,
                                   admit=None, model=self.entries is None, bootstrap=True)
        return strategy(gate, events)


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
        if args.routing_provider == "jevrouter":
            validate_config(args.jev_url, args.timeout)
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

    supervisor = AttemptSupervisor(task, BrowserExecutionStrategy(task, args, entries),
                                   verifier_factory=FixtureVerifier)
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: supervisor.cancel())
    try:
        result = supervisor.run()
    finally:
        signal.signal(signal.SIGINT, previous)
    result.update(trace_ref="trace.jsonl", evidence_refs=list(dict.fromkeys(
        [ref for row in result["verification"] for ref in row.get("evidence_refs", [])] +
        [ref for row in result["actions"] for ref in row.get("evidence_refs", [])])))
    try:
        (output / "trace.jsonl").write_text("".join(json.dumps({"event": row}, ensure_ascii=True) + "\n"
                                                 for row in result.get("trace", result["actions"])), encoding="utf-8")
        # Evidence contains only identifiers and assessments, never raw fixture state or slots.
        evidence = [{"ref": ref, "criterion_id": row["id"], "status": row["status"],
                     "observed_at": row.get("observed_at")}
                    for row in result["verification"] for ref in row.get("evidence_refs", [])]
        evidence += [{"ref": ref, "action_id": row["id"], "effect": row["effect"]}
                     for row in result["actions"] for ref in row.get("evidence_refs", [])
                     if ref.startswith("browser:")]
        (output / "evidence.jsonl").write_text("".join(
            json.dumps(row, ensure_ascii=True) + "\n" for row in evidence), encoding="utf-8")
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    except OSError:
        result.update(attempt_status="failed", stop_reason="output_error", task_outcome="unknown")
        print(json.dumps(result, ensure_ascii=True))
        return EXIT_CODES["failed"]
    print(json.dumps(result, ensure_ascii=True))
    return EXIT_CODES[result["attempt_status"]]
