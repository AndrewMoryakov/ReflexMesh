"""Decision-only adapter for the local JevRouter HTTP service."""
import hashlib
import http.client
import json
import math
import time
from urllib.parse import urlsplit

from reflexmesh.contracts.task import Route, Task, ValidationError

MAX_RESPONSE_BYTES = 1024 * 1024
DESCRIPTIONS = {
    Route.CUA: "Interact with a browser or desktop UI: navigate, click or fill a field. Routing only; no action is executed.",
    Route.LLM: "Reason, analyze, plan or generate text/code. Return control to the calling reasoning agent.",
    Route.PERCEPTION: "Extract information from an existing screenshot or observation without interacting with the UI.",
}


class AdapterError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("non-finite number")


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def validate_config(endpoint: str, timeout: float) -> tuple[str, int]:
    try:
        parts = urlsplit(endpoint)
        port = 8787 if parts.port is None else parts.port
        valid = (parts.scheme == "http" and parts.hostname == "127.0.0.1"
                 and not parts.username and not parts.password and not parts.query
                 and not parts.fragment and parts.path in ("", "/") and 1 <= port <= 65535)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValidationError("JevRouter endpoint must be http://127.0.0.1:PORT")
    if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValidationError("timeout must be finite, greater than 0 and at most 120 seconds")
    return "127.0.0.1", port


def _post(host: str, port: int, payload: bytes, timeout: float) -> bytes:
    # Direct loopback connection: no environment proxy, redirect or automatic retry.
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("POST", "/route", body=payload,
                           headers={"Content-Type": "application/json", "Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise AdapterError("http_error")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise AdapterError("response_too_large")
        return body
    except TimeoutError as exc:
        raise AdapterError("transport_timeout") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise AdapterError("transport_error") from exc
    finally:
        connection.close()


def _parse(raw: bytes, eligible: tuple[Route, ...], allow_demo: bool) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
        if type(value) is not dict:
            raise ValueError()
        if value["mode"] != "decision_only" or value["execution"] != {"enabled": False, "status": "not_started"}:
            raise ValueError()
        # Reject 0 in place of a boolean, which Python dict equality otherwise accepts.
        if value["execution"]["enabled"] is not False:
            raise ValueError()
        provider = value["provenance"]["jev_provider"]
        if provider not in ("typesafe", "openrouter:~typesafe/jev-latest", "jevrouter-demo"):
            raise AdapterError("unsupported_provider")
        if provider == "jevrouter-demo" and not allow_demo:
            raise AdapterError("demo_not_allowed")
        for key in ("request_id", "decision_id"):
            if type(value[key]) is not str or not value[key] or len(value[key]) > 256:
                raise ValueError()
        if value.get("error") is not None:
            # Never relay upstream error messages: they may contain request data or keys.
            raise AdapterError("provider_error")
        status = value["status"]
        if status not in ("selected", "needs_confirmation", "no_decision"):
            raise ValueError()
        decision = value["decision"]
        if decision["kind"] != "choice":
            raise ValueError()
        rows = decision["candidates"]
        expected = {r.value for r in eligible}
        if type(rows) is not list or len(rows) != len(expected):
            raise ValueError()
        by_id = {}
        for row in rows:
            name = row["id"]
            if type(name) is not str or name not in expected or name in by_id:
                raise ValueError()
            if not _number(row["jev_probability"]) or not _number(row["jev_confidence"]):
                raise ValueError()
            flags = row["router"]
            for flag in ("available", "allowed", "filtered", "requires_confirmation"):
                if type(flags[flag]) is not bool:
                    raise ValueError()
            by_id[name] = row
        selected, choice = decision["selected"], decision["jev_choice"]
        if choice is not None and (type(choice) is not str or choice not in expected):
            raise ValueError()
        if status == "no_decision":
            if selected is not None:
                raise ValueError()
        else:
            if type(selected) is not str or selected not in by_id or choice is None:
                raise ValueError()
            flags = by_id[selected]["router"]
            if flags["filtered"] or not flags["allowed"] or not flags["available"]:
                raise ValueError()
            if (status == "needs_confirmation") != flags["requires_confirmation"]:
                raise ValueError()
        fallback = value["fallback"]["type"]
        if fallback not in (None, "manual_review", "provider_error", "no_safe_candidate", "low_confidence"):
            raise ValueError()
        if fallback == "provider_error":
            raise AdapterError("provider_error")
        # Only the provider's original confidence is exposed; no synthesized confidence.
        raw_jev = value.get("raw_jev")
        confidence = None
        if type(raw_jev) is dict:
            answer = raw_jev.get("answers", {}).get("tool", {})
            confidence = answer.get("confidence")
            if confidence is not None and not _number(confidence):
                raise ValueError()
        return {"status": status, "selected": selected, "provider": provider,
                "jev_choice": choice, "confidence": confidence, "fallback_type": fallback,
                "probabilities": {name: row["jev_probability"] for name, row in by_id.items()},
                "decision_id": value["decision_id"], "request_id": value["request_id"]}
    except AdapterError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError) as exc:
        raise AdapterError("invalid_response") from exc


def route_task(task: Task, *, endpoint: str = "http://127.0.0.1:8787",
               timeout: float = 30.0, allow_demo: bool = False) -> dict:
    host, port = validate_config(endpoint, timeout)
    if type(allow_demo) is not bool:
        raise ValidationError("allow_demo must be boolean")
    eligible = tuple(r for r in Route if r in task.capabilities and r in task.allowed_routes)
    result = {"schema_version": "0.2", "task_id": task.task_id, "status": "abstained",
              "route": None, "eligible_routes": [r.value for r in eligible],
              "reason_code": "no_eligible_route", "provider": "jevrouter",
              "is_stub": False, "execution_performed": False, "confidence": None,
              "trace": {"request_sent": False, "response_received": False,
                        "request_sha256": None, "response_sha256": None,
                        "elapsed_ms": 0, "upstream": None}}
    if not eligible:
        return result
    payload = json.dumps({"request": task.goal, "actor_permissions": [],
                          "candidates": [{"id": r.value, "name": r.value, "type": "subagent",
                                          "description": DESCRIPTIONS[r],
                                          "verification": {"status": "unverified", "source": "reflexmesh_declared"},
                                          "availability": {"available": True},
                                          "risk": {"level": "low", "categories": ["routing_only"]},
                                          "execution": {"mode": "custom", "dry_run": True}}
                                         for r in eligible]}, ensure_ascii=True).encode()
    trace = result["trace"]
    trace["request_sha256"] = hashlib.sha256(payload).hexdigest()
    start = time.monotonic()
    try:
        # request_sent means attempted transport, not confirmed upstream receipt.
        trace["request_sent"] = True
        raw = _post(host, port, payload, timeout)
        trace["response_received"] = True
        trace["response_sha256"] = hashlib.sha256(raw).hexdigest()
        parsed = _parse(raw, eligible, allow_demo)
        trace["upstream"] = parsed
        result["is_stub"] = parsed["provider"] == "jevrouter-demo"
        result["confidence"] = parsed["confidence"]
        status = parsed["status"]
        if status == "selected":
            result.update(status="selected", route=parsed["selected"], reason_code="upstream_selected")
        elif status == "needs_confirmation":
            result.update(status="needs_confirmation", reason_code="upstream_confirmation_required")
        else:
            result["reason_code"] = "upstream_no_decision"
    except AdapterError as exc:
        result.update(status="failed", reason_code=exc.code)
        if exc.code == "demo_not_allowed":
            result["is_stub"] = True
    trace["elapsed_ms"] = round((time.monotonic() - start) * 1000, 3)
    return result
