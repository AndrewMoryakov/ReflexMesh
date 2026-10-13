"""`reflexmesh browser run`: one controlled attempt of a browser-task/0.1 (V0.6)."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from pathlib import Path

from reflexmesh.contracts.browser import BrowserTask, loads_strict
from reflexmesh.contracts.task import ValidationError
from reflexmesh.runtime.browser_run import BrowserRun

EXIT_CODES = {"completed": 0, "blocked": 3, "incomplete": 4, "failed": 5, "cancelled": 130}


def _error(code: str) -> int:
    print(json.dumps({"error": {"code": code}}, ensure_ascii=True), file=sys.stderr)
    return 2


def _read(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read(64 * 1024 + 1)
    with open(path, "rb") as stream:
        return stream.read(64 * 1024 + 1)


def _script_steps(raw: object) -> list[dict]:
    if (type(raw) is not list or not raw or len(raw) > 200 or
            any(type(step) is not dict or type(step.get("action")) is not str or
                any(type(v) is not str for v in step.values()) for step in raw)):
        raise ValidationError("a script is a nonempty list of {action, <parameter>: <pattern>} steps")
    return raw


def provider_setup(args) -> tuple:
    """(provider_factory, checks) from the run/serve arguments; raises ValueError on invalid input.

    A script for the script provider is a list of steps, or (for a session service, where one chain has several
    attempts) a list of such lists: each attempt takes the next one."""
    if args.action_provider == "laya" and args.model:
        raise ValidationError("the laya provider has no model choice")
    scripts = None
    if args.action_provider == "script":
        if not args.script:
            raise ValidationError("the script provider requires --script")
        raw = loads_strict(_read(args.script))
        scripts = ([_script_steps(item) for item in raw] if type(raw) is list and raw and type(raw[0]) is list
                   else [_script_steps(raw)])
    elif args.script:
        raise ValidationError("--script requires the script provider")
    checks = {}
    if getattr(args, "checks", None):
        from reflexmesh.verification.checks import load_checks

        checks = load_checks(args.checks)
    attempts = [0]

    def provider_factory():
        if scripts is not None:
            from reflexmesh.adapters.system_one.decision_providers import LabelScriptProvider

            steps = scripts[min(attempts[0], len(scripts) - 1)]
            attempts[0] += 1
            return LabelScriptProvider(steps), "script:label", None
        if args.action_provider == "laya":
            from reflexmesh.adapters.system_one.laya_provider import laya_provider

            provider, config_id = laya_provider(getattr(args, "decisions_url", None))
            return provider, config_id, provider.model
        from reflexmesh.adapters.system_one.decision_providers import jev_provider_from_env

        provider, config_id = jev_provider_from_env(args.model, base_url=getattr(args, "decisions_url", None))
        return provider, config_id, provider.model

    return provider_factory, checks


def run_browser(args) -> int:
    try:
        task = BrowserTask.from_json(_read(args.input))
        provider_factory, checks = provider_setup(args)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return _error("invalid_input")
    output = Path(args.output_dir)
    try:
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            return _error("output_unavailable")
    except OSError:
        return _error("output_unavailable")

    chrome = args.chrome or os.environ.get("REFLEXMESH_CHROME")
    sandbox = not (args.no_sandbox or os.environ.get("REFLEXMESH_CHROME_NO_SANDBOX") == "1")
    run = BrowserRun(task, chrome=chrome, provider_factory=provider_factory, action_provider=args.action_provider,
                     out_dir=output, sandbox=sandbox, attempt_id=args.attempt_id, checks=checks)
    signal.signal(signal.SIGINT, lambda *_: run.cancel())
    signal.signal(signal.SIGTERM, lambda *_: run.cancel())
    try:
        result = run.run()
    finally:
        signal.signal(signal.SIGINT, lambda *_: None)
    print(json.dumps(result, ensure_ascii=True))
    return EXIT_CODES[result["attempt_status"]]


def serve_browser(args) -> int:
    """`reflexmesh browser serve`: runs until `shutdown`, SIGTERM or SIGINT; every open session is closed on exit."""
    from reflexmesh.runtime.browser_core import BrowserCore
    from reflexmesh.runtime.browser_service import SessionService

    try:
        provider_factory, checks = provider_setup(args)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return _error("invalid_input")
    chrome = args.chrome or os.environ.get("REFLEXMESH_CHROME")
    sandbox = not (args.no_sandbox or os.environ.get("REFLEXMESH_CHROME_NO_SANDBOX") == "1")
    state = Path(args.state_dir)
    try:
        state.mkdir(parents=True, exist_ok=True)
        os.chmod(state, 0o700)
    except OSError:
        return _error("output_unavailable")
    core = BrowserCore(chrome=chrome, provider_factory=provider_factory, action_provider=args.action_provider,
                       out_dir=state, sandbox=sandbox, checks=checks, lease_seconds=args.lease_seconds)
    service = SessionService(core, args.socket)
    try:
        service.start()
    except (OSError, FileExistsError):
        return _error("socket_unavailable")
    signal.signal(signal.SIGINT, lambda *_: threading.Thread(target=service.shutdown, daemon=True).start())
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=service.shutdown, daemon=True).start())
    print(json.dumps({"listening": str(args.socket)}), flush=True)
    service.serve_forever()
    return 0


def call_service(args) -> int:
    """`reflexmesh browser submit|resume|cancel|status|shutdown`: one request to a running service."""
    from reflexmesh.runtime.browser_service import call

    op = {"submit": "execute"}.get(args.browser_command, args.browser_command)
    request: dict = {"op": op}
    try:
        if op == "execute":
            request["task"] = loads_strict(_read(args.input))
        elif op == "resume":
            request["continuation"] = loads_strict(_read(args.input))
        elif op == "cancel":
            request["chain_id"] = args.chain_id
    except (OSError, UnicodeError, ValueError, RecursionError):
        return _error("invalid_input")
    try:
        response = call(args.socket, request)
    except (OSError, ValueError):
        return _error("service_unavailable")
    print(json.dumps(response, ensure_ascii=True))
    if "error" in response:
        return 2
    return EXIT_CODES.get(response.get("attempt_status"), 0)
