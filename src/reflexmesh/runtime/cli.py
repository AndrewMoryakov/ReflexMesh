"""Execution CLI: validate input, verify fixture identity, then supervise one attempt."""

from __future__ import annotations

import functools
import importlib.util
import json
import math
import re
import signal
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser
from reflexmesh.adapters.system_one.harness import HarnessStrategy
from reflexmesh.adapters.system_one.script_selector import FixtureScriptProvider, validate_script
from reflexmesh.contracts.execution import ExecutionTask, Limits, PREDICATES
from reflexmesh.contracts.task import Route, Task, ValidationError
from reflexmesh.routing.jev_router import validate_config
from reflexmesh.routing.router import Router, execution_router
from reflexmesh.runtime.faults import FaultBarriers, validate_faults
from reflexmesh.runtime.handoff import build_handoff, chain_usage, standalone_chain
from reflexmesh.runtime.runner import AttemptSupervisor, WorkerResult
from reflexmesh.runtime.ownership import OwnershipError
from reflexmesh.verification.verifier import FixtureVerifier

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


ATTEMPT_ID = re.compile(r"^[0-9a-f]{32}$")


def validate_chain(raw, task) -> dict:
    """Link to a parent attempt: identity, root limits, and what the chain already used.

    A disabled (null) root limit is unlimited; the task may then use any value for it.
    """
    if type(raw) is not dict or set(raw) != {"root_attempt_id", "parent_attempt_id", "sequence",
                                             "root_limits", "used"}:
        raise ValidationError("chain must have exactly root_attempt_id, parent_attempt_id, sequence, root_limits, used")
    if not all(type(raw[k]) is str and ATTEMPT_ID.match(raw[k]) for k in ("root_attempt_id", "parent_attempt_id")):
        raise ValidationError("chain attempt IDs must be 32 lowercase hex characters")
    if type(raw["sequence"]) is not int or raw["sequence"] < 2:
        raise ValidationError("chain sequence must be an integer >= 2")
    limits, used = raw["root_limits"], raw["used"]
    if (type(limits) is not dict or set(limits) != {"wall_seconds", "max_steps", "max_model_calls", "max_action_retries"}
            or type(used) is not dict or set(used) != {"steps", "model_calls", "wall_seconds"}):
        raise ValidationError("invalid chain limits or usage")
    for key, value in (*limits.items(), *used.items()):
        if value is None and key in ("wall_seconds", "max_steps", "max_model_calls"):
            continue
        if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValidationError("chain numbers must be finite and nonnegative")
    if any(value is None for value in used.values()):
        raise ValidationError("chain usage must be numeric")
    # A linked attempt may never exceed what the chain has left.
    left = {"max_steps": None if limits["max_steps"] is None else limits["max_steps"] - used["steps"],
            "max_model_calls": (None if limits["max_model_calls"] is None
                                else limits["max_model_calls"] - used["model_calls"]),
            "wall_seconds": (None if limits["wall_seconds"] is None
                             else limits["wall_seconds"] - used["wall_seconds"] + 1e-6)}
    for key, remaining in left.items():
        wanted = getattr(task.limits, key)
        if remaining is not None and (wanted is None or wanted > remaining):
            raise ValidationError("task limits exceed the chain's remaining budget")
    return raw


def _jev_action_provider():
    from systemone_harness.provider import provider_from_env
    provider = provider_from_env()
    provider.retries = 0  # No opaque HTTP retries outside the model-call budget.
    return provider


def _script_provider(entries):
    return FixtureScriptProvider(entries)


def claim_fixture(task, timeout, owner):
    """The fixture captures its baseline inside the exclusive claim transaction."""
    return owner.acquire(timeout)


class BrowserExecutionStrategy:
    """Run preflight, macro routing and the browser loop in one supervised worker."""

    def __init__(self, task, args, entries, *, router: Router | None = None, faults=None):
        self.task, self.args, self.entries = task, args, entries
        self.router = router if router is not None else execution_router(args.routing_provider, args.jev_url)
        self.faults = validate_faults(faults or {}, limits=task.limits)

    def __call__(self, gate, events):
        task, args = self.task, self.args
        gate.remaining()
        if any(c.predicate not in PREDICATES for c in task.criteria):
            return WorkerResult("blocked", "unsupported_criterion")
        if "browser.soh" not in task.allowed_executors:
            return WorkerResult("blocked", "executor_unavailable")
        try:
            receipt = claim_fixture(task, timeout=gate.operation_timeout(2.0), owner=gate.fixture_owner)
        except OwnershipError as exc:
            return WorkerResult("blocked", exc.reason)
        except ValueError:
            return WorkerResult("blocked", "fixture_mismatch")
        except (OSError, json.JSONDecodeError):
            return WorkerResult("blocked", "executor_unavailable")
        events.put(("fixture_claim", receipt))  # Private IPC; never written to the trace.
        gate.remaining()

        routing_task = Task("0.1", task.task_id, task.goal, (Route.CUA,), (Route.CUA,))
        accounting = self.router.capabilities.model_call_accounting
        gate.begin_routing(accounting)
        events.put(("router_capabilities", self.router.router_id, accounting))
        outcome = self.router.route(routing_task, timeout=gate.operation_timeout(args.timeout),
                                    reserve_call=gate.reserve_call)
        routing = outcome.decision
        events.put(("router_usage", outcome.usage.model_calls))
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
        barriers = (FaultBarriers(self.faults, gate, events, control_dir=getattr(args, "control_dir", None))
                    if self.faults else None)
        strategy = HarnessStrategy(task.goal, functools.partial(FixtureBrowser, task, chrome=chrome),
                                   FixtureBrowser.action_space,
                                   functools.partial(_script_provider, self.entries) if self.entries is not None
                                   else _jev_action_provider,
                                   admit=None, model=self.entries is None, bootstrap=True, faults=barriers)
        return strategy(gate, events)


def run_execution(args) -> int:
    try:
        task = ExecutionTask.from_dict(_load(args.input))
        if getattr(args, "no_limits", False):
            task = replace(task, limits=Limits(None, None, None, 0))
        faults = (validate_faults(_load(args.faults), limits=task.limits)
                  if getattr(args, "faults", None) else {})
        attempt_id = getattr(args, "attempt_id", None)
        if attempt_id is not None and not ATTEMPT_ID.match(attempt_id):
            raise ValidationError("attempt ID must be 32 lowercase hex characters")
        chain = validate_chain(_load(args.chain), task) if getattr(args, "chain", None) else None
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

    supervisor = AttemptSupervisor(task, BrowserExecutionStrategy(task, args, entries, faults=faults),
                                   verifier_factory=FixtureVerifier, attempt_id=attempt_id)
    signal.signal(signal.SIGINT, lambda *_: supervisor.cancel())
    control = Path(args.control_dir) if getattr(args, "control_dir", None) else None
    if control is not None:
        # Handshake with a Task API caller: announce the handler, then honour a cancel that was
        # requested before it existed (the caller only signals once 'ready' is present).
        (control / "ready").touch()
        if (control / "cancel").exists():
            supervisor.cancel()
    try:
        result = supervisor.run()
    finally:
        # A late SIGINT after the terminal transition is a no-op; it must not interrupt the output.
        signal.signal(signal.SIGINT, lambda *_: None)
    # Configuration is not proof that a barrier was reached; the redacted
    # `fault` trace events record which hooks actually ran.
    result["faults"] = faults
    result.update(trace_ref="trace.jsonl", evidence_persistence="stored", evidence_refs=list(dict.fromkeys(
        [ref for row in result["verification"] for ref in row.get("evidence_refs", [])] +
        [ref for row in result["actions"] for ref in row.get("evidence_refs", [])] +
        [row["ref"] for row in result.get("ownership_evidence", []) if "ref" in row])))
    root_limits = {"wall_seconds": task.limits.wall_seconds, "max_steps": task.limits.max_steps,
                   "max_model_calls": task.limits.max_model_calls, "max_action_retries": 0}
    # A result without a chain link (an injected supervisor, for instance) is a standalone attempt.
    result.setdefault("chain", standalone_chain(str(result.get("attempt_id") or attempt_id or "")))
    if chain:
        result["chain"] = {k: chain[k] for k in ("root_attempt_id", "parent_attempt_id", "sequence")}
        root_limits = chain["root_limits"]
    usage = chain_usage(chain["used"] if chain else None, result)
    result["chain"]["used"] = usage
    result["handoff"] = build_handoff(task, result, root_limits=root_limits, usage=usage)
    try:
        (output / "trace.jsonl").write_text("".join(json.dumps({"event": row}, ensure_ascii=True) + "\n"
                                                 for row in result.get("trace", result["actions"])), encoding="utf-8")
        # Evidence contains only identifiers and assessments, never raw fixture state or slots.
        # Slot refs resolve to actual retained, redacted receipts/coverage below;
        # copying an assessment row alone is not driver-boundary evidence.
        slot_records = result.get("slot_evidence", [])
        ownership_records = result.get("ownership_evidence", [])
        fixture_records = list({row["fixture_snapshot"]["ref"]: row["fixture_snapshot"]
                                for row in result["verification"] if "fixture_snapshot" in row}.values())
        retained_refs = {row["ref"] for row in slot_records + ownership_records + fixture_records if "ref" in row}
        evidence = [{"ref": ref, "criterion_id": row["id"], "status": row["status"],
                     **({"scope": row["scope"]} if "scope" in row else {}),
                     **({"reason": row["reason"]} if "reason" in row else {}),
                     "observed_at": row.get("observed_at")}
                    for row in result["verification"] for ref in row.get("evidence_refs", [])
                    if ref not in retained_refs]
        evidence += slot_records + ownership_records + fixture_records
        evidence += [{"ref": ref, "action_id": row["id"], "effect": row["effect"]}
                     for row in result["actions"] for ref in row.get("evidence_refs", [])
                     if ref.startswith("browser:")]
        (output / "evidence.jsonl").write_text("".join(
            json.dumps(row, ensure_ascii=True) + "\n" for row in evidence), encoding="utf-8")
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    except OSError:
        result.update(attempt_status="failed", stop_reason="output_error",
                      task_outcome="fail" if result.get("task_outcome") == "fail" else "unknown",
                      evidence_persistence="unavailable", trace_ref=None, evidence_refs=[], slot_evidence=[],
                      ownership_evidence=[])
        # Preserve a confirmed violation, but never advertise references to files
        # whose complete publication failed. Partial files are not acceptance.
        for row in result["verification"]:
            row["evidence_refs"] = []
            row.pop("fixture_snapshot", None)
            if row.get("status") == "pass":
                row.update(status="unknown", reason="Evidence persistence failed.")
        for row in result["actions"]:
            row["evidence_refs"] = []
        result["handoff"] = build_handoff(task, result, root_limits=root_limits, usage=usage)
        print(json.dumps(result, ensure_ascii=True))
        return EXIT_CODES["failed"]
    print(json.dumps(result, ensure_ascii=True))
    return EXIT_CODES[result["attempt_status"]]
