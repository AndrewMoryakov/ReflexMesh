"""`reflexmesh browser run`: one controlled attempt of a browser-task/0.1 (V0.6)."""

from __future__ import annotations

import json
import os
import signal
import sys
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


def run_browser(args) -> int:
    try:
        task = BrowserTask.from_json(_read(args.input))
        if args.action_provider == "laya" and args.model:
            raise ValidationError("the laya provider has no model choice")
        if args.action_provider == "script":
            if not args.script:
                raise ValidationError("the script provider requires --script")
            steps = _script_steps(loads_strict(_read(args.script)))
        elif args.script:
            raise ValidationError("--script requires the script provider")
        else:
            steps = None
        checks = {}
        if getattr(args, "checks", None):
            from reflexmesh.verification.checks import load_checks

            checks = load_checks(args.checks)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return _error("invalid_input")
    output = Path(args.output_dir)
    try:
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            return _error("output_unavailable")
    except OSError:
        return _error("output_unavailable")

    def provider_factory():
        if steps is not None:
            from reflexmesh.adapters.system_one.decision_providers import LabelScriptProvider

            return LabelScriptProvider(steps), "script:label", None
        if args.action_provider == "laya":
            from reflexmesh.adapters.system_one.laya_provider import laya_provider

            provider, config_id = laya_provider(getattr(args, "decisions_url", None))
            return provider, config_id, provider.model
        from reflexmesh.adapters.system_one.decision_providers import jev_provider_from_env

        provider, config_id = jev_provider_from_env(args.model, base_url=getattr(args, "decisions_url", None))
        return provider, config_id, provider.model

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
