"""`reflexmesh browser serve`: a local session service around `BrowserCore` (V0.6 section 14).

A one-shot `browser run` holds no browser after it returns, so it cannot offer continuation. This service is the
long-lived process that does: it keeps the chains, their open browser sessions, leases and tokens, and answers the
same three operations as the embedding API, `execute`, `resume` and `cancel`, plus `status` and `shutdown`. It adds
no policy of its own: tokens, leases, budgets and success rules all stay in the core.

Transport: a Unix-domain socket, one JSON request line and one JSON response line per connection. There is no HTTP
API (section 14). The socket is created mode 0600 and every connection's peer must be the same user (SO_PEERCRED);
anyone who can reach it acts as that user, which is why it is not a network port. Connections are served in threads,
so a `cancel` gets through while an `execute` of the same chain is still running.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import stat
import struct
import threading
from pathlib import Path

from reflexmesh.contracts.browser import BrowserTask
from reflexmesh.contracts.task import ValidationError

MAX_REQUEST = 1 << 20
SERVICE_SCHEMA = "browser-service/0.1"


def _error(code: str, **detail) -> dict:
    return {"schema_version": "browser-error/0.1", "error": {"code": code, **detail}}


class SessionService:
    def __init__(self, core, socket_path: str | Path):
        self.core = core
        self.path = Path(socket_path)
        self.server: socketserver.ThreadingUnixStreamServer | None = None
        self._stopping = threading.Event()

    # ── requests ──
    def handle(self, request: object) -> dict:
        if type(request) is not dict or type(request.get("op")) is not str:
            return _error("invalid_request")
        op = request["op"]
        if op == "execute":
            try:
                task = BrowserTask.from_dict(request.get("task"))
            except (ValidationError, ValueError, TypeError, AttributeError):
                return _error("invalid_task")
            return self.core.execute(task)
        if op == "resume":
            return self.core.resume(request.get("continuation"))
        if op == "cancel":
            return self.core.cancel(str(request.get("chain_id") or ""))
        if op == "status":
            return self.status()
        if op == "shutdown":
            threading.Thread(target=self.shutdown, daemon=True).start()
            return {"schema_version": SERVICE_SCHEMA, "shutting_down": True}
        return _error("unknown_op")

    def status(self) -> dict:
        chains = []
        for chain in list(self.core.chains.values()):
            chains.append({"chain_id": chain.chain_id, "task_id": chain.task.task_id, "revision": chain.task.revision,
                           "attempts": list(chain.attempts), "session_open": chain.session is not None,
                           "assisting": chain.assist is not None, "closed": chain.closed,
                           "running": chain.chain_id in self.core.running, "remaining": chain.remaining()})
        return {"schema_version": SERVICE_SCHEMA, "chains": chains}

    # ── transport ──
    def _bind(self) -> socketserver.ThreadingUnixStreamServer:
        service = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                creds = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                _, uid, _ = struct.unpack("3i", creds)
                if uid != os.getuid():
                    return   # another user: no answer at all
                line = self.rfile.readline(MAX_REQUEST + 1)
                if not line:
                    return   # a liveness probe: connected and left
                if len(line) > MAX_REQUEST:
                    response = _error("request_too_large")
                else:
                    try:
                        request = json.loads(line)
                    except ValueError:
                        response = _error("invalid_request")
                    else:
                        try:
                            response = service.handle(request)
                        except Exception as exc:   # the message may hold page text: only its type goes out
                            response = _error("service_error", type=type(exc).__name__)
                try:
                    self.wfile.write((json.dumps(response, ensure_ascii=True, default=str) + "\n").encode())
                except OSError:
                    pass     # the caller went away; the work itself is not undone

        if self.path.exists() or self.path.is_symlink():
            mode = self.path.lstat().st_mode
            if not stat.S_ISSOCK(mode):
                raise FileExistsError("socket path exists and is not a socket")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(self.path))
            except OSError:
                self.path.unlink()          # a stale socket from a service that is gone
            else:
                probe.close()
                raise FileExistsError("another service is listening on this socket")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        old = os.umask(0o177)
        try:
            server = socketserver.ThreadingUnixStreamServer(str(self.path), Handler)
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        server.daemon_threads = True
        return server

    def start(self) -> None:
        self.server = self._bind()

    def serve_forever(self) -> None:
        if self.server is None:
            self.start()
        try:
            self.server.serve_forever(poll_interval=0.2)
        finally:
            self._close()

    def shutdown(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        for chain_id in list(self.core.running):
            self.core.cancel(chain_id)
        if self.server is not None:
            self.server.shutdown()

    def _close(self) -> None:
        self.core.close()
        if self.server is not None:
            self.server.server_close()
        try:
            self.path.unlink()
        except OSError:
            pass


def call(socket_path: str | Path, request: dict, timeout: float | None = None) -> dict:
    """Client side: one request, one response."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(socket_path))
        conn.sendall((json.dumps(request, ensure_ascii=True) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data or b"{}")
