"""MCP stdio server for the V0.6 Task API (newline-delimited JSON-RPC 2.0, no dependencies).

Tools: reflexmesh_submit, reflexmesh_status, reflexmesh_cancel, reflexmesh_continue. Requests are
handled on worker threads so a bounded status wait never delays a cancellation. Only JSON-RPC
messages are written to stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import json
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from reflexmesh.api.service import MAX_WAIT_SECONDS, ApiError, TaskService
from reflexmesh.routing.jev_router import validate_config

SERVER_VERSION = "0.6.0"
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "ReflexMesh executes one bounded browser subtask per attempt under runtime control and verifies it. "
    "Submit with reflexmesh_submit, then poll reflexmesh_status (wait_seconds up to 50) until state=terminal. "
    "Only attempt_status=completed with task_outcome=pass is success. Any other result carries a handoff: "
    "read handoff.kind, handoff.needs and handoff.actions (done / not_done / possibly_done). Never repeat a "
    "possibly_done action. ReflexMesh never writes text itself: if handoff.kind=needs_content, prepare the "
    "content and call reflexmesh_continue with add_text_slots (a new slot version) and matching criteria.")

_TASK = {"type": "object", "description": "execution-task/0.1 object (see docs/specs/V0.5.md section 2)"}
_SCRIPT = {"type": "array", "items": {"type": "object"},
           "description": "deterministic script entries; required with action_provider=script only"}
_PROVIDERS = {"routing_provider": {"type": "string", "enum": ["stub", "jevrouter"]},
              "action_provider": {"type": "string", "enum": ["script", "jev"]}}
TOOLS = [
    {"name": "reflexmesh_submit",
     "description": "Start one bounded, verified browser subtask. Returns an attempt_id immediately.",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"task": _TASK, **_PROVIDERS, "script": _SCRIPT},
                     "required": ["task", "routing_provider", "action_provider"]},
     "annotations": {"title": "Submit subtask", "readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": False, "openWorldHint": False}},
    {"name": "reflexmesh_status",
     "description": "State of an attempt; waits up to wait_seconds for it to finish. Terminal results include "
                    "verification, actions with effects, budget, chain and, unless completed, a handoff.",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"attempt_id": {"type": "string"},
                                    "wait_seconds": {"type": "number", "minimum": 0, "maximum": MAX_WAIT_SECONDS},
                                    "detail": {"type": "string", "enum": ["summary", "full"]}},
                     "required": ["attempt_id"]},
     "annotations": {"title": "Attempt status", "readOnlyHint": True, "idempotentHint": True,
                     "openWorldHint": False}},
    {"name": "reflexmesh_cancel",
     "description": "Request cancellation of a running attempt. Acceptance is decided at the attempt's dispatch "
                    "gate; an action already dispatched may still take effect. Check status afterwards.",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"attempt_id": {"type": "string"}}, "required": ["attempt_id"]},
     "annotations": {"title": "Cancel attempt", "readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": False}},
    {"name": "reflexmesh_continue",
     "description": "Start the next linked attempt after a handoff, with the chain's remaining budget. "
                    "changes may set goal, add_text_slots, criteria, start_path, fixture, and narrow permissions. "
                    "Refused when the parent has a possibly_done action or the budget is exhausted.",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"parent_attempt_id": {"type": "string"},
                                    "changes": {"type": "object"}, **_PROVIDERS, "script": _SCRIPT},
                     "required": ["parent_attempt_id", "changes", "routing_provider", "action_provider"]},
     "annotations": {"title": "Continue after handoff", "readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": False, "openWorldHint": False}},
]
_FAULTS = {"type": "object", "description": "acceptance fault barriers (server started with --allow-faults)"}


def tool_list(allow_faults: bool) -> list:
    if not allow_faults:
        return TOOLS
    import copy
    tools = copy.deepcopy(TOOLS)
    for tool in tools:
        if tool["name"] in ("reflexmesh_submit", "reflexmesh_continue"):
            tool["inputSchema"]["properties"]["faults"] = _FAULTS
    return tools


class Server:
    def __init__(self, service: TaskService, out=None):
        self.service = service
        self.tools = tool_list(service.allow_faults)
        self.tool_args = {tool["name"]: (set(tool["inputSchema"]["properties"]), set(tool["inputSchema"]["required"]))
                          for tool in self.tools}
        self.out = out or sys.stdout
        self.write_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mcp")
        self.initialized = False

    def send(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=True, separators=(",", ":"))
        with self.write_lock:
            self.out.write(line + "\n")
            self.out.flush()

    def _error(self, request_id, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def call_tool(self, name: str, arguments) -> dict:
        if name not in self.tool_args:
            raise ApiError("unknown_tool", f"no tool {name}")
        allowed, required = self.tool_args[name]
        if type(arguments) is not dict or set(arguments) - allowed or required - set(arguments):
            raise ApiError("invalid_input", f"{name} takes {sorted(allowed)}; required {sorted(required)}")
        s = self.service
        if name == "reflexmesh_submit":
            return s.submit(arguments["task"], arguments["routing_provider"], arguments["action_provider"],
                            arguments.get("script"), arguments.get("faults"))
        if name == "reflexmesh_status":
            return s.status(arguments["attempt_id"], arguments.get("wait_seconds", 0),
                            arguments.get("detail", "summary"))
        if name == "reflexmesh_cancel":
            return s.cancel(arguments["attempt_id"])
        return s.continue_task(arguments["parent_attempt_id"], arguments["changes"],
                               arguments["routing_provider"], arguments["action_provider"], arguments.get("script"),
                               arguments.get("faults"))

    def handle(self, message) -> None:
        if type(message) is not dict or message.get("jsonrpc") != "2.0" or type(message.get("method")) is not str:
            if type(message) is dict and "id" in message and "method" not in message:
                return  # A response to a request we never send; ignore.
            return self._error(message.get("id") if type(message) is dict else None, -32600, "invalid request")
        method, request_id, params = message["method"], message.get("id"), message.get("params") or {}
        notification = "id" not in message
        if method.startswith("notifications/"):
            if method == "notifications/initialized":
                self.initialized = True
            return
        if notification:
            return
        if method == "initialize":
            requested = params.get("protocolVersion") if type(params) is dict else None
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return self.send({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "reflexmesh", "version": SERVER_VERSION}, "instructions": INSTRUCTIONS}})
        if method == "ping":
            return self.send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        if method == "tools/list":
            return self.send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.tools}})
        if method == "tools/call":
            if type(params) is not dict or type(params.get("name")) is not str:
                return self._error(request_id, -32602, "tools/call requires a tool name")
            try:
                payload, is_error = self.call_tool(params["name"], params.get("arguments", {})), False
            except ApiError as exc:
                payload, is_error = exc.to_dict(), True
            except Exception as exc:  # noqa: BLE001 - reported to the client, never crashes the server
                payload, is_error = {"error": {"code": "internal_error", "message": type(exc).__name__}}, True
            return self.send({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=1)}],
                "structuredContent": payload, "isError": is_error}})
        return self._error(request_id, -32601, f"method not found: {method}")

    def dispatch(self, line: str) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return self._error(None, -32700, "parse error")
        if type(message) is list:
            return self._error(None, -32600, "batches are not supported")
        self.pool.submit(self._safe_handle, message)

    def _safe_handle(self, message) -> None:
        try:
            self.handle(message)
        except Exception as exc:  # noqa: BLE001
            print(f"reflexmesh mcp: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    def close(self) -> dict:
        self.pool.shutdown(wait=False, cancel_futures=True)
        return self.service.shutdown()


def serve(args) -> int:
    validate_config(args.jev_url, 30.0)
    service = TaskService(args.state_dir, chrome=args.chrome, jev_url=args.jev_url,
                          max_concurrent=args.max_concurrent, allow_faults=args.allow_faults)
    server = Server(service)

    class Terminate(Exception):
        pass

    def terminate(*_):
        raise Terminate()  # Interrupts the blocking stdin read in the main thread.

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        for line in sys.stdin:
            if line.strip():
                server.dispatch(line)
    except (Terminate, ValueError, OSError):
        pass
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # Shutdown below is itself bounded.
        summary = server.close()
        print(f"reflexmesh mcp: shutdown {json.dumps(summary)}", file=sys.stderr, flush=True)
    return 0
