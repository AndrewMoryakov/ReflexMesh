"""The reviewed SOH -> Browser Use text handoff, with an actual-payload witness.

SOH ab8e8f08 / browser-use 5c892e01 (0.13.10): ``BrowserEnvironment`` has a
synchronous ``_run`` bridge and builds ``_action_model(input=...)`` for
``_tools.act(model, _session)``. The backend's mutable ``text_values`` and its
``_execute`` / ``_act`` helpers are intentionally never used for text dispatch.

The witness covers exactly the argument handed to Tools.act. DOM/focus behavior,
driver internals, and the eventual field/server value are separate contracts.
"""

from __future__ import annotations

import inspect
import logging

from reflexmesh.runtime.runner import RuntimeStop
from reflexmesh.text.slots import PreparedTextCommand

BOUNDARY = "systemone.browser_use.tools.act.input/v1"


def _unsupported():
    return RuntimeStop("adapter_contract_unsupported")


def dispatch_text(backend, command: PreparedTextCommand, action_id: int, sink):
    """Dispatch a resolved immutable command, never an untrusted parameter map."""
    try:
        if (type(command) is not PreparedTextCommand or
                not callable(getattr(backend, "_run", None)) or
                not callable(getattr(sink, "handoff", None))):
            raise _unsupported()
        # Do not start a browser or silently take a different async path here.
        # The fresh observation already started the pinned backend/session.
        return backend._run(_dispatch_text(backend, command, action_id, sink))
    except RuntimeStop as exc:
        if exc.reason in ("adapter_contract_unsupported", "effect_unknown"):
            raise
        raise RuntimeStop("effect_unknown") from None
    except Exception:
        # The bridge and dynamically supplied model properties can themselves
        # fail with a text-bearing error. Never expose it to the controller.
        raise RuntimeStop("effect_unknown") from None


async def _dispatch_text(backend, command: PreparedTextCommand, action_id: int, sink):
    driver = getattr(backend, "_tools", None)
    act = getattr(driver, "act", None)
    session = getattr(backend, "_session", None)
    model_factory = getattr(backend, "_action_model", None)
    if session is None or not callable(model_factory) or not callable(act):
        raise _unsupported()
    try:
        # The pinned time_execution_sync decorator is a synchronous wrapper
        # around async Tools.act; functools.wraps preserves its reviewed target.
        unwrapped = inspect.unwrap(act)
        if not inspect.iscoroutinefunction(unwrapped):
            raise _unsupported()
        if getattr(unwrapped, "__module__", None) == "browser_use.tools.service":
            # The reviewed Tools.act always includes params in a Laminar span
            # when Laminar imports successfully. Its input handler also logs
            # raw text at DEBUG. Neither is part of our redacted evidence path.
            namespace = getattr(unwrapped, "__globals__", {})
            logger = namespace.get("logger")
            if ("Laminar" not in namespace or namespace["Laminar"] is not None or
                    not isinstance(logger, logging.Logger) or logger.isEnabledFor(logging.DEBUG)):
                raise _unsupported()
        inspect.signature(act).bind(object(), session, sensitive_data=None)
        model = model_factory(input={"index": command.index, "text": command._value, "clear": True})
        payload = model.model_dump(exclude_unset=True)
        input_model = model.input
    except Exception:
        # Upstream model validation errors can contain the complete value.
        raise _unsupported() from None
    if (type(payload) is not dict or set(payload) != {"input"} or
            type(payload["input"]) is not dict or
            set(payload["input"]) != {"index", "text", "clear"}):
        raise _unsupported()
    data = payload["input"]
    if (type(data["index"]) is not int or data["index"] != command.index or
            data["clear"] is not True or
            type(getattr(input_model, "index", None)) is not int or
            input_model.index != command.index or getattr(input_model, "clear", None) is not True):
        raise _unsupported()
    # Inspect BOTH the actual field and the serialization Tools.act consumes.
    # A serializer/model mutation must never turn a prepared intent into a pass.
    actual = getattr(input_model, "text", None)
    if type(actual) is not str or actual != command._value:
        raise _unsupported()
    actual = data["text"]
    if type(actual) is not str or actual != command._value:
        raise _unsupported()
    try:
        # Make the actual call FIRST. A worker killed before argument handoff
        # must not leave a receipt that falsely claims the call happened.
        pending = act(model, session, sensitive_data=None)
    except Exception:
        raise RuntimeStop("effect_unknown") from None
    if not inspect.isawaitable(pending):
        raise _unsupported()
    # No await, rebuild, lookup, or transformation separates the validation,
    # SAME-object argument handoff and witness. This proves the call argument,
    # not coroutine completion, focus, DOM delivery, or an external effect.
    sink.handoff(action_id, command.reference, actual, boundary=BOUNDARY)
    try:
        result = await pending
        from systemone_harness.environment import Result

        # Browser Use returns typed text in extracted_content/errors. Do not
        # relay those strings into SOH steps or default artifacts.
        ok = not result.error
        return Result(ok=ok, text="Text action returned." if ok else "Text action failed.")
    except Exception:
        # Even a driver exception means this exact payload was handed off. Its
        # effect is unknown; neither its text nor its repr belongs in SOH traces.
        raise RuntimeStop("effect_unknown") from None
