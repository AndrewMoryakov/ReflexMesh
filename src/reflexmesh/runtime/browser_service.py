"""`reflexmesh browser serve`: a local session service around `BrowserCore` (V0.6 section 14).

A one-shot `browser run` holds no browser after it returns, so it cannot offer continuation. This service is the
long-lived process that does: it keeps the chains, their open browser sessions, leases and tokens, and answers the
same three operations as the embedding API, `execute`, `resume` and `cancel`, plus `status` and `shutdown`. It adds
no policy of its own: tokens, leases, budgets and success rules all stay in the core.

Transport: a Unix-domain socket, one JSON request line and one JSON response line per connection. There is no HTTP
API (section 14). The socket is created mode 0600 and every connection's peer must be the same user (SO_PEERCRED);
anyone who can reach it acts as that user, which is why it is not a network port. Connections are served in threads,
so a `cancel` gets through while an `execute` of the same chain is still running.

Windows has no Unix sockets in CPython and no SO_PEERCRED. There the service listens on a named pipe with a random
name and authenticates every connection with a random key (multiprocessing's HMAC challenge); the socket path holds
an address file with both, readable only by the current user (its ACL is reset to that user alone). Whoever can read
that file acts as the user, as with the socket. Still no network port; the pipe accepts SMB clients only with the key.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import socketserver
import stat
import struct
import subprocess
import threading
from pathlib import Path

from reflexmesh.contracts.browser import BrowserTask
from reflexmesh.contracts.task import ValidationError

MAX_REQUEST = 1 << 20
SERVICE_SCHEMA = "browser-service/0.1"
WINDOWS = os.name == "nt"


def _error(code: str, **detail) -> dict:
    return {"schema_version": "browser-error/0.1", "error": {"code": code, **detail}}


class SessionService:
    def __init__(self, core, socket_path: str | Path):
        self.core = core
        self.path = Path(socket_path)
        self.server: socketserver.ThreadingUnixStreamServer | None = None
        self.listener = None                         # Windows: a multiprocessing PipeListener
        self._address: dict | None = None
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

    def respond(self, raw: bytes) -> bytes:
        """One request's bytes to one response's bytes (both transports)."""
        if len(raw) > MAX_REQUEST:
            response = _error("request_too_large")
        else:
            try:
                request = json.loads(raw)
            except ValueError:
                response = _error("invalid_request")
            else:
                try:
                    response = self.handle(request)
                except Exception as exc:   # the message may hold page text: only its type goes out
                    response = _error("service_error", type=type(exc).__name__)
        return (json.dumps(response, ensure_ascii=True, default=str) + "\n").encode()

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
                try:
                    self.wfile.write(service.respond(line))
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

    def _bind_pipe(self) -> None:
        from multiprocessing.connection import Listener

        if self.path.exists() or self.path.is_symlink():
            if _read_address(self.path) is None:
                raise FileExistsError("socket path exists and is not a service address file")
            try:
                call(self.path, None)           # connects and leaves: a liveness probe
            except (OSError, EOFError, ValueError):
                self.path.unlink()             # the address of a service that is gone
            else:
                raise FileExistsError("another service is listening on this address")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        name = r"\\.\pipe\reflexmesh-browser-" + secrets.token_hex(16)
        self.listener = Listener(name, family="AF_PIPE", authkey=key)
        self._address = {"schema_version": SERVICE_SCHEMA, "pipe": name, "key": key.hex()}
        open(self.path, "x").close()           # empty until only this user can read it
        try:
            _private_file(self.path)
        except Exception:
            self.path.unlink()
            self.listener.close()
            raise
        self.path.write_text(json.dumps(self._address), encoding="ascii")

    def start(self) -> None:
        if WINDOWS:
            self._bind_pipe()
        else:
            self.server = self._bind()

    def serve_forever(self) -> None:
        if self.server is None and self.listener is None:
            self.start()
        try:
            if self.listener is not None:
                self._serve_pipe()
            else:
                self.server.serve_forever(poll_interval=0.2)
        finally:
            self._close()

    def _serve_pipe(self) -> None:
        from multiprocessing import AuthenticationError

        while not self._stopping.is_set():
            try:
                conn = self.listener.accept()
            except (AuthenticationError, EOFError, OSError):
                continue                       # a caller without the key, or one that left during the handshake
            if self._stopping.is_set():
                conn.close()
                break
            threading.Thread(target=self._serve_connection, args=(conn,), daemon=True).start()

    def _serve_connection(self, conn) -> None:
        try:
            try:
                raw = conn.recv_bytes(MAX_REQUEST + 1)
            except OSError:
                raw = b"x" * (MAX_REQUEST + 1)   # longer than the limit
            except EOFError:
                return                         # a liveness probe: connected and left
            try:
                conn.send_bytes(self.respond(raw))
            except (OSError, EOFError):
                pass
        finally:
            conn.close()

    def shutdown(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        for chain_id in list(self.core.running):
            self.core.cancel(chain_id)
        if self.server is not None:
            self.server.shutdown()
        if self.listener is not None:
            try:
                call(self.path, None)          # wakes the accept loop, which then sees the flag
            except (OSError, EOFError, ValueError):
                pass

    def _close(self) -> None:
        self.core.close()
        if self.server is not None:
            self.server.server_close()
        if self.listener is not None:
            self.listener.close()
        try:
            self.path.unlink()
        except OSError:
            pass


def _read_address(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="ascii"))
    except (OSError, ValueError, UnicodeError):
        return None
    if (type(data) is not dict or data.get("schema_version") != SERVICE_SCHEMA or type(data.get("pipe")) is not str
            or not data["pipe"].startswith("\\\\.\\pipe\\") or type(data.get("key")) is not str):
        return None
    return data


def _private_file(path: Path) -> None:
    """Windows: the file's ACL becomes the current user alone (inheritance removed), named by SID: under an SSH
    logon USERDOMAIN can be the workgroup, which icacls cannot map to an account."""
    who = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True)
    sid = who.stdout.strip().rsplit(",", 1)[-1].strip().strip('"')
    if who.returncode != 0 or not sid.startswith("S-1-"):
        raise PermissionError("no current user to restrict the address file to")
    done = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:F"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise PermissionError("could not restrict the address file")


def call(socket_path: str | Path, request: dict | None, timeout: float | None = None) -> dict:
    """Client side: one request, one response. `request=None` only connects and leaves (a liveness probe)."""
    if WINDOWS:
        from multiprocessing.connection import Client

        address = _read_address(Path(socket_path))
        if address is None:
            raise FileNotFoundError("no service address at this path")
        conn = Client(address["pipe"], family="AF_PIPE", authkey=bytes.fromhex(address["key"]))
        try:
            if request is None:
                return {}
            conn.send_bytes((json.dumps(request, ensure_ascii=True) + "\n").encode())
            if timeout is not None and not conn.poll(timeout):
                raise TimeoutError("no response from the service")
            return json.loads(conn.recv_bytes() or b"{}")
        finally:
            conn.close()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(socket_path))
        if request is None:
            return {}
        conn.sendall((json.dumps(request, ensure_ascii=True) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data or b"{}")
