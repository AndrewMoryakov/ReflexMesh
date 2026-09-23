"""JSON CLI for routing-only V0.1. No executor or model is invoked."""
import argparse
import json
import sys
from pathlib import Path

from reflexmesh.contracts.task import Task, ValidationError
from reflexmesh.routing.stub import route_task

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
    parser = Parser(prog="reflexmesh", description="V0.1 routing stub; no task execution")
    parser.add_argument("--version", action="version", version="reflexmesh 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)
    route = sub.add_parser("route", help="select a route using an explicit fixed-order stub")
    route.add_argument("--input", default="-", metavar="FILE", help="JSON file or - for stdin")
    try:
        args = parser.parse_args(argv)
        decision = route_task(read_task(args.input))
    except OSError:
        error = {"code": "input_read_error", "message": "Unable to read task input"}
    except (ValidationError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        # Decoder diagnostics can contain input; do not echo the task into errors.
        message = str(exc) if isinstance(exc, ValidationError) else "Invalid JSON or UTF-8 input"
        error = {"code": "invalid_input", "message": message}
    else:
        # ASCII escaping preserves all Unicode values even on non-UTF-8 terminals.
        print(json.dumps(decision.to_dict(), ensure_ascii=True))
        return 0 if decision.status == "selected" else 3
    print(json.dumps({"error": error}, ensure_ascii=True), file=sys.stderr)
    return 2
