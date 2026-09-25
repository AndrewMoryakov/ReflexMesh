"""JSON CLI for routing only. JevRouter is opt-in; no executor is invoked."""
import argparse
import json
import sys
from pathlib import Path

from reflexmesh.contracts.task import Task, ValidationError
from reflexmesh.routing.stub import route_task
from reflexmesh.routing.jev_router import route_task as jev_route, validate_config

MAX_INPUT_BYTES = 64 * 1024


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValidationError(message)


def unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("duplicate JSON object key")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValidationError("non-finite JSON number is not supported")


def read_task(path: str) -> Task:
    if path == "-":
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValidationError("input exceeds 64 KiB")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                      parse_constant=reject_constant)
    return Task.from_dict(data)


def main(argv: list[str] | None = None) -> int:
    parser = Parser(prog="reflexmesh", description="Routing only: explicit stub or local JevRouter")
    parser.add_argument("--version", action="version", version="reflexmesh 0.2.0")
    sub = parser.add_subparsers(dest="command", required=True)
    route = sub.add_parser("route", help="select a route using an explicit fixed-order stub")
    route.add_argument("--input", default="-", metavar="FILE", help="JSON file or - for stdin")
    route.add_argument("--provider", choices=("stub", "jevrouter"), default="stub")
    route.add_argument("--jev-url", default="http://127.0.0.1:8787")
    route.add_argument("--timeout", type=float, default=30.0, help="socket I/O timeout in seconds")
    route.add_argument("--allow-demo", action="store_true", help="explicitly accept upstream demo responses")
    route.add_argument("--no-none-candidate", action="store_true",
                       help="do not offer the NONE refusal candidate to JevRouter (pre-ADR-0003 behaviour)")
    run = sub.add_parser("run", help="execute one bounded browser subtask")
    run.add_argument("--input", default="-", metavar="FILE")
    run.add_argument("--output-dir", required=True, metavar="DIR")
    run.add_argument("--routing-provider", choices=("stub", "jevrouter"), required=True)
    run.add_argument("--action-provider", choices=("script", "jev"), required=True)
    run.add_argument("--script", metavar="FILE")
    run.add_argument("--chrome", metavar="FILE", help="explicit Chromium/Chrome executable")
    run.add_argument("--jev-url", default="http://127.0.0.1:8787")
    run.add_argument("--timeout", type=float, default=30.0)
    try:
        args = parser.parse_args(argv)
        if args.command == "run":
            from reflexmesh.runtime.cli import run_execution
            return run_execution(args)
        if args.provider == "stub" and (args.allow_demo or args.no_none_candidate
                                        or args.jev_url != "http://127.0.0.1:8787" or args.timeout != 30.0):
            raise ValidationError("JevRouter options require --provider jevrouter")
        if args.provider == "jevrouter":
            validate_config(args.jev_url, args.timeout)
        task = read_task(args.input)
        decision = (jev_route(task, endpoint=args.jev_url, timeout=args.timeout, allow_demo=args.allow_demo,
                              none_candidate=not args.no_none_candidate)
                    if args.provider == "jevrouter" else route_task(task).to_dict())
    except OSError:
        error = {"code": "input_read_error", "message": "Unable to read task input"}
    except (ValidationError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        # Decoder diagnostics can contain input; do not echo the task into errors.
        message = str(exc) if isinstance(exc, ValidationError) else "Invalid JSON or UTF-8 input"
        error = {"code": "invalid_input", "message": message}
    else:
        # ASCII escaping preserves all Unicode values even on non-UTF-8 terminals.
        print(json.dumps(decision, ensure_ascii=True))
        return {"selected": 0, "abstained": 3, "needs_confirmation": 4, "failed": 5}[decision["status"]]
    print(json.dumps({"error": error}, ensure_ascii=True), file=sys.stderr)
    return 2
