"""A read-only local view of one browser run: who decided each step, what, and how long it took.

It reads the run directory's `trace.jsonl` (written line by line while the run is going, already
redacted of slot values) and `result.json` once it exists. It never writes, never talks to a
browser or a provider, and serves only two things on loopback: the page and one JSON document.

Timing comes from the journal's own clock (`t`, seconds since the attempt began, millisecond
resolution), so every figure is a difference between two recorded events:
  observe   previous event -> `observed`         building the observation, incl. the page capture
  decision  `decision_requested` -> `decision_received`   the whole provider round trip as we saw it
  admission `proposal` -> `dispatch_committed` / `admission_rejected`   re-checking the live page
  browser   `dispatch_committed` -> `driver_returned`   the command itself
The provider's own figure (`latency_ms` of the response) is shown next to the decision time.
"""

from __future__ import annotations

import json
import secrets
import statistics
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

MAX_TRACE_BYTES = 32 * 1024 * 1024


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))


def read_run(run_dir: Path) -> tuple[list[dict], dict | None]:
    """The records written so far and the final result, if there is one. A half-written last line is skipped."""
    records: list[dict] = []
    trace = run_dir / "trace.jsonl"
    try:
        if trace.is_file() and trace.stat().st_size <= MAX_TRACE_BYTES:
            for line in trace.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("seq"), int):
                    records.append(row)
    except OSError:
        pass
    result = None
    try:
        data = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        result = data if isinstance(data, dict) else None
    except (OSError, ValueError):
        pass
    return records, result


def _label(options: list[dict], value) -> str | None:
    """The role and name of the option a decision pointed at (options are keyed `obs:kind:key`)."""
    for option in options:
        parts = str(option.get("ref", "")).split(":", 2)
        if len(parts) == 3 and parts[2] == str(value):
            return f"{option.get('role') or option.get('operation')} {option.get('name')!r}".replace("''", "").strip()
    return None


def build_view(records: list[dict], result: dict | None) -> dict:
    meta: dict = {}
    steps: list[dict] = []
    by_action: dict[int, dict] = {}
    current: dict | None = None
    last_t = 0.0
    for row in sorted(records, key=lambda r: r["seq"]):
        kind, t = row.get("kind"), float(row.get("t") or 0.0)
        if kind == "task_accepted":
            meta.update(task_id=row.get("task_id"), revision=row.get("revision"), limits=row.get("limits"))
        elif kind == "attempt_started":
            meta.update(attempt_id=row.get("attempt_id"), provider=row.get("provider"),
                        config_id=row.get("config_id"), requested_model=row.get("requested_model"))
        elif kind == "observed":
            current = {"n": len(steps) + 1, "observation_id": row.get("observation_id"), "page": row.get("page"),
                       "coverage": (row.get("coverage") or {}).get("status"),
                       "coverage_reasons": (row.get("coverage") or {}).get("reasons") or [],
                       "options": [], "t": round(t, 3), "observe_ms": _ms(t - last_t)}
            steps.append(current)
        elif kind == "dispatch_committed" and current is None:
            meta["start"] = {"action_id": row.get("action_id"), "destination": row.get("destination"), "t": t}
            by_action[row.get("action_id")] = {"start": True, "dispatch_t": t}
        elif kind == "driver_returned" and by_action.get(row.get("action_id"), {}).get("start"):
            owner = by_action[row["action_id"]]
            meta["start"]["driver_ms"] = _ms(t - owner["dispatch_t"])
            meta["start"]["ok"] = row.get("ok") is True and not row.get("error")
        elif current is None:
            pass
        elif kind == "options_built":
            current["options"] = row.get("options") or []
        elif kind == "decision_requested":
            current["decision_t"] = t
            current["requested_model"] = row.get("requested_model")
            current["offered"] = row.get("options")
        elif kind in ("decision_received", "decision_failed"):
            current["decision_ms"] = _ms(t - current.get("decision_t", t))
            if kind == "decision_received":
                current["decision"] = {
                    "author": row.get("served_model"), "requested_model": row.get("requested_model"),
                    "request_id": row.get("request_id"), "provider_ms": row.get("latency_ms"),
                    "usage": row.get("usage"), "action": row.get("action"), "params": row.get("params") or {},
                    "confidence": row.get("confidence"), "goal_reached": row.get("goal_reached"),
                    # answers a translating provider filled in itself; the model did not make these
                    "filled_by_translator": (row.get("translated") or {}).get("derived") or {}}
            else:
                current["decision_error"] = {"error": row.get("error"), "status": row.get("status"),
                                             "protocol": row.get("protocol")}
                current["outcome"] = {"kind": "decision_failed"}
        elif kind == "threshold_judged":
            current["judged"] = {"verdict": row.get("verdict"), "weakest": row.get("weakest"),
                                 "threshold": row.get("threshold")}
            if row.get("verdict") in ("finish", "escalate", "refused"):
                current["outcome"] = {"kind": row["verdict"]}
        elif kind == "proposal":
            current["proposal_t"] = t
        elif kind == "admission_rejected":
            current["admission_ms"] = _ms(t - current.get("proposal_t", t))
            current["outcome"] = {"kind": "rejected", "reason": row.get("reason")}
        elif kind == "dispatch_committed":
            current["admission_ms"] = _ms(t - current.get("proposal_t", t))
            current["dispatch"] = {k: row.get(k) for k in ("action_id", "operation", "risk", "option_ref", "target",
                                                           "slot_ref", "destination", "gate_seq")}
            current["dispatch_t"] = t
            current["outcome"] = {"kind": "dispatched"}
            by_action[row.get("action_id")] = current
        elif kind == "driver_returned":
            owner = by_action.get(row.get("action_id"))
            if owner is not None:
                owner["driver_ms"] = _ms(t - owner.get("dispatch_t", t))
                good = row.get("ok") is True and not row.get("error")
                owner["outcome"] = {"kind": "done" if good else "driver_error"}
        elif kind == "effect_assessed":
            owner = by_action.get(row.get("action_id"))
            if owner is not None and not owner.get("start"):
                owner["effect"] = {k: v for k, v in row.items() if k not in ("seq", "t", "kind", "action_id")}
        elif kind == "terminal":
            meta["terminal"] = {k: row.get(k) for k in ("attempt_status", "stop_reason", "handoff_reason")}
            meta["total_ms"] = _ms(t)
        last_t = t

    terminated = bool(meta.get("terminal")) or bool(result)
    for step in steps:
        if not step.get("decision") and not step.get("decision_error") and "outcome" not in step:
            # No decision was asked for here: either the look after `finish`, or one still being made.
            step["kind"] = "final_observation" if terminated and step is steps[-1] else "awaiting_decision"
        decision = step.get("decision")
        filled = (decision or {}).get("filled_by_translator") or {}
        step["chosen"] = [] if not decision else [
            {"param": name, "value": value, "label": _label(step["options"], value),
             **({"by": "translator", "how": filled[f"{decision['action']}__{name}"]}
                if f"{decision['action']}__{name}" in filled else {})}
            for name, value in decision["params"].items()]
        for key in ("decision_t", "proposal_t", "dispatch_t"):
            step.pop(key, None)

    decided = [s["decision_ms"] for s in steps if "decision_ms" in s]
    ordered = sorted(decided)
    total_ms = meta.get("total_ms") or (_ms(float(records[-1].get("t") or 0)) if records else 0)
    summary = {
        "steps": len(steps), "decisions": len(decided),
        "decision_ms_total": sum(decided),
        "decision_ms_median": round(statistics.median(decided)) if decided else None,
        "decision_ms_p95": ordered[min(len(ordered) - 1, max(0, -(-95 * len(ordered) // 100) - 1))] if ordered else None,
        "decision_ms_max": max(decided) if decided else None,
        "decision_share": round(sum(decided) / total_ms, 3) if decided and total_ms else None,
        "run_ms": total_ms,
        "authors": _count(s["decision"]["author"] for s in steps if s.get("decision")),
        "outcomes": _count((s.get("outcome") or {}).get("kind") for s in steps),
    }
    state = ("waiting" if not records else "finished" if meta.get("terminal") or result else "running")
    shown = None
    if result:
        shown = {k: result.get(k) for k in ("attempt_status", "stop_reason", "task_outcome", "execution_outcome")}
        shown["budget"] = {k: (result.get("budget") or {}).get(k) for k in ("steps", "model_calls", "dispatches")}
        shown["decision"] = result.get("decision")
    return {"state": state, "meta": meta, "summary": summary, "steps": steps, "result": shown}


def _count(values) -> dict:
    counts: dict = {}
    for value in values:
        if value is not None:
            counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def make_server(run_dir: Path, port: int = 8780) -> ThreadingHTTPServer:
    from reflexmesh.runtime.browser_view_page import PAGE

    class Handler(BaseHTTPRequestHandler):
        server_version = "reflexmesh-view"

        def log_message(self, *args):
            pass

        def _host_ok(self) -> bool:
            # A page in another tab must not be able to read this through a rebinding trick.
            return (self.headers.get("Host") or "") in (f"127.0.0.1:{self.server.server_port}",
                                                        f"localhost:{self.server.server_port}")

        def _send(self, code: int, body: bytes, ctype: str, nonce: str | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; "
                             + (f"script-src 'nonce-{nonce}'" if nonce else "script-src 'none'"))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain; charset=utf-8")
            path = urlsplit(self.path).path
            if path == "/":
                nonce = secrets.token_urlsafe(16)
                return self._send(200, PAGE.replace("__NONCE__", nonce).encode("utf-8"),
                                  "text/html; charset=utf-8", nonce)
            if path == "/api/run":
                records, result = read_run(run_dir)
                data = json.dumps(build_view(records, result), ensure_ascii=False).encode("utf-8")
                return self._send(200, data, "application/json; charset=utf-8")
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def _refuse(self):
            self._send(405, b"read-only", "text/plain; charset=utf-8")

        do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(args) -> int:
    run_dir = Path(args.run_dir)
    if not run_dir.parent.exists():
        print(json.dumps({"error": {"code": "invalid_input"}}), file=sys.stderr)
        return 2
    server = make_server(run_dir, args.port)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"ReflexMesh run view (read-only, loopback): {url}\nwatching {run_dir}  -  Ctrl+C to stop", flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0
