"""V0.5 loopback fixture with exclusive, explicitly released attempt claims.

    python server.py --port 8765 --run-id example

GET /__state is redacted health metadata. Claim before using browser routes.
Claims have no expiry: revoke fences effects; release requires confirmed cleanup.
"""
import argparse
import copy
import hashlib
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import re
import threading
import time
from urllib.parse import parse_qs
import uuid

LOCK = threading.Lock()
STATE: dict = {}
LOG: list = []
RUN_ID = ""
SEQUENCE = 0
INSTANCE_ID = ""
OWNER_EPOCH = 0
CLAIM: dict | None = None
PENDING_EFFECTS: dict = {}
REVOKED_CLAIMS: set = set()
IDENTITY_FIELDS = frozenset({"task_id", "task_revision", "attempt_id", "run_id", "claim_id"})
SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
MAX_BODY_BYTES = 1024 * 1024


def reset() -> None:
    global SEQUENCE, INSTANCE_ID, CLAIM
    with LOCK:
        STATE.clear()
        STATE.update({"notify_email": False, "settings_saved": 0, "form": None, "account_deleted": False,
                      "exports": 0})
        LOG.clear()
        SEQUENCE = 0
        # A reset invalidates every previously issued binding, even in this process.
        INSTANCE_ID = str(uuid.uuid4())
        CLAIM = None
        PENDING_EFFECTS.clear()
        REVOKED_CLAIMS.clear()


def _identity(value: object) -> bool:
    return (type(value) is dict and set(value) == IDENTITY_FIELDS and
            type(value["task_revision"]) is int and value["task_revision"] > 0 and
            all(type(value[k]) is str and 0 < len(value[k]) <= 256 and value[k].strip()
                for k in IDENTITY_FIELDS - {"task_revision"}))


def _public_id(value: object) -> bool:
    # Browser identity is an opaque public ID, never a user-data directory.
    return (type(value) is str and 0 < len(value) <= 256 and value.isprintable() and
            value == value.strip() and not any(c in value for c in ("/", "\\", ":")) and
            value not in (".", ".."))


def _same_secret(value: object, expected: str) -> bool:
    return (type(value) is str and SECRET_PATTERN.fullmatch(value) is not None and
            hmac.compare_digest(value, expected))


def _claim_key(identity: dict, owner_secret: str) -> tuple:
    return (tuple(identity[key] for key in sorted(IDENTITY_FIELDS)),
            hashlib.sha256(owner_secret.encode("ascii")).digest())


def _baseline_locked() -> dict:
    return {"run_id": RUN_ID, "instance_id": INSTANCE_ID, "sequence": SEQUENCE,
            "state": copy.deepcopy(STATE), "log": copy.deepcopy(LOG)}


def _snapshot_locked(*, private: bool) -> dict:
    value = _baseline_locked()
    value["owner_epoch"] = OWNER_EPOCH
    value["phase"] = CLAIM["phase"] if CLAIM is not None else "unclaimed"
    value["binding"] = copy.deepcopy(CLAIM["binding"]) if CLAIM is not None else None
    if private:
        value["effects"] = [copy.deepcopy(event) for event in LOG
                            if event["method"] == "POST" and CLAIM is not None and
                            event["seq"] > CLAIM["baseline"]["sequence"]]
        value["pending_effects"] = [copy.deepcopy(event) for event in PENDING_EFFECTS.values()
                                    if CLAIM is not None and event["binding"] == CLAIM["binding"]]
        value["baseline"] = copy.deepcopy(CLAIM["baseline"]) if CLAIM is not None else None
    elif value["state"]["form"] is not None:
        value["state"]["form"] = {"submitted": True}
    return value


def _append_locked(method: str, path: str, binding: dict) -> None:
    global SEQUENCE
    SEQUENCE += 1
    # Raw form values and both bearer secrets stay out of every event record.
    LOG.append({"seq": SEQUENCE, "t": time.time(), "method": method, "path": path,
                "binding": copy.deepcopy(binding)})


NAV = ('<nav><a data-reflex-id="home" href="/">Home</a> | '
       '<a data-reflex-id="profile" href="/profile">Profile</a> | '
       '<a data-reflex-id="settings" href="/settings">Settings</a> | '
       '<a data-reflex-id="reports" href="/reports">Reports</a> | '
       '<a data-reflex-id="form" href="/form">Contact form</a> | '
       '<a data-reflex-id="danger" href="/danger">Danger zone</a> | '
       '<a data-reflex-id="export" href="/slow">Export</a></nav>')


def page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{escape(title)}</title></head>"
            f"<body>{NAV}<h1>{escape(title)}</h1>{body}</body></html>").encode()


def render(path: str) -> bytes | None:
    s = STATE
    if path == "/":
        return page("Home", "<p>Welcome to the test site. Use the menu to open a section.</p>")
    if path == "/profile":
        return page("Profile", "<p>Name: Test User. Plan: Basic.</p>")
    if path == "/reports":
        return page("Reports", "<p>Monthly report for May: 120 orders, revenue 4,800.</p>")
    if path == "/settings":
        checked = " checked" if s["notify_email"] else ""
        saved = "<p role='status'>Settings saved.</p>" if s["settings_saved"] else ""
        return page("Settings", f"{saved}<form method='post' action='/settings'>"
                    f"<label><input data-reflex-id='notify-email' type='checkbox' name='notify_email'{checked}>"
                    " Email notifications</label> "
                    "<button data-reflex-id='save-settings' type='submit'>Save</button></form>")
    if path == "/form":
        sent = ""
        if s["form"]:
            sent = f"<p role='status'>Message sent from {escape(s['form']['name'])}.</p>"
        return page("Contact form", f"{sent}<form method='post' action='/form'>"
                    "<label>Name <input data-reflex-id='name' type='text' name='name'></label> "
                    "<label>Email <input data-reflex-id='email' type='email' name='email'></label> "
                    "<button data-reflex-id='send-form' type='submit'>Send</button></form>")
    if path == "/danger":
        gone = "<p role='status'>Account deleted.</p>" if s["account_deleted"] else ""
        return page("Danger zone", f"{gone}<p>Deleting the account cannot be undone.</p>"
                    "<form method='post' action='/danger/delete'>"
                    "<button data-reflex-id='delete-account' type='submit'>Delete account</button></form>")
    if path == "/slow":
        done = f"<p role='status'>Exports finished: {s['exports']}.</p>" if s["exports"] else ""
        return page("Export", f"{done}<p>Exporting takes a few seconds.</p>"
                    "<form method='post' action='/slow/export'>"
                    "<button data-reflex-id='start-export' type='submit'>Start export</button></form>")
    return None


class Handler(BaseHTTPRequestHandler):
    slow_seconds = 3.0

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, value: dict) -> None:
        self._send(code, json.dumps(value).encode(), "application/json")

    def _redirect(self, to: str) -> None:
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _local_request(self) -> bool:
        expected = f"127.0.0.1:{self.server.server_port}"
        origin = self.headers.get("Origin")
        return self.headers.get("Host") == expected and origin in (None, f"http://{expected}")

    def _browser_binding_locked(self) -> dict | None:
        if (CLAIM is None or CLAIM["phase"] != "active" or
                "session_id" not in CLAIM["binding"] or "profile_id" not in CLAIM["binding"]):
            return None
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            cookie = cookies.get("ReflexMeshOwner")
        except CookieError:
            return None
        if cookie is None or not _same_secret(cookie.value, CLAIM["browser_secret"]):
            return None
        return copy.deepcopy(CLAIM["binding"])

    def _owner_locked(self, payload: dict, *, recovery: bool = False) -> bool:
        if CLAIM is None or not _same_secret(payload.get("owner_secret"), CLAIM["owner_secret"]):
            return False
        if "binding" in payload:
            # Exact equality also rejects partial/stale browser identity bindings.
            supplied = payload["binding"]
            expected = CLAIM["binding"]
            return (type(supplied) is dict and set(supplied) == set(expected) and
                    all(type(supplied[key]) is type(value) and supplied[key] == value
                        for key, value in expected.items()))
        # A lost acquisition/bind response must not strand the parent without a
        # way to fence its claim. Secret plus exact initial identity is sufficient
        # only for reading/revoking, never binding or releasing a different epoch.
        return recovery and _identity(payload.get("identity")) and payload["identity"] == CLAIM["identity"]

    def _control_locked(self, path: str, payload: dict) -> tuple[int, dict]:
        global CLAIM, OWNER_EPOCH
        if path == "/__claim":
            identity = payload.get("identity")
            owner_secret, browser_secret = payload.get("owner_secret"), payload.get("browser_secret")
            if (set(payload) != {"identity", "owner_secret", "browser_secret"} or
                    not _identity(identity) or
                    any(type(secret) is not str or SECRET_PATTERN.fullmatch(secret) is None
                        for secret in (owner_secret, browser_secret)) or owner_secret == browser_secret):
                return 400, {"error": "invalid_claim"}
            if identity["run_id"] != RUN_ID:
                return 409, {"error": "fixture_mismatch"}
            if _claim_key(identity, owner_secret) in REVOKED_CLAIMS:
                return 409, {"error": "claim_revoked"}
            if CLAIM is not None and CLAIM["phase"] != "released":
                return 409, {"error": "fixture_owned"}
            # The first owner and its baseline are established in one critical
            # section. There is deliberately no time-based release or takeover.
            baseline = _baseline_locked()
            OWNER_EPOCH += 1
            binding = {**identity, "instance_id": INSTANCE_ID, "owner_epoch": OWNER_EPOCH}
            CLAIM = {"identity": copy.deepcopy(identity), "binding": binding,
                     "owner_secret": owner_secret, "browser_secret": browser_secret,
                     "phase": "active", "baseline": baseline}
            return 200, {"binding": copy.deepcopy(binding), "baseline": copy.deepcopy(baseline),
                         "phase": "active"}
        if path not in {"/__owner/state", "/__owner/bind", "/__owner/revoke", "/__owner/release"}:
            return 404, {"error": "not_found"}
        identity, owner_secret = payload.get("identity"), payload.get("owner_secret")
        if (path in {"/__owner/state", "/__owner/revoke"} and "binding" not in payload and
                _identity(identity) and identity["run_id"] == RUN_ID and
                type(owner_secret) is str and SECRET_PATTERN.fullmatch(owner_secret) is not None and
                (CLAIM is None or CLAIM["identity"] != identity)):
            # Cancellation may reach the server before its acquisition request.
            # Fence that exact parent identity+secret without touching a distinct
            # live owner, and reject the delayed request whenever it arrives.
            key = _claim_key(identity, owner_secret)
            if path == "/__owner/revoke":
                REVOKED_CLAIMS.add(key)
            if key in REVOKED_CLAIMS:
                return 200, {"phase": "revoked_before_claim", "identity": copy.deepcopy(identity),
                             "instance_id": INSTANCE_ID}
        if not self._owner_locked(payload, recovery=path in {"/__owner/state", "/__owner/revoke"}):
            return 403, {"error": "owner_mismatch"}
        if path == "/__owner/bind":
            if CLAIM["phase"] != "active":
                return 409, {"error": "claim_fenced"}
            if "session_id" in CLAIM["binding"] or "profile_id" in CLAIM["binding"]:
                return 409, {"error": "already_bound"}
            if not _public_id(payload.get("session_id")) or not _public_id(payload.get("profile_id")):
                return 400, {"error": "invalid_browser_identity"}
            CLAIM["binding"] = {**CLAIM["binding"], "session_id": payload["session_id"],
                                "profile_id": payload["profile_id"]}
        elif path == "/__owner/revoke":
            if CLAIM["phase"] == "released":
                return 409, {"error": "claim_released"}
            REVOKED_CLAIMS.add(_claim_key(CLAIM["identity"], CLAIM["owner_secret"]))
            CLAIM["phase"] = "quarantined"
        elif path == "/__owner/release":
            if CLAIM["phase"] != "quarantined":
                return 409, {"error": "claim_not_quarantined"}
            # The trusted parent asserts this only after OwnedWorker cleanup is
            # positively clean. Unknown/failed cleanup must keep quarantine.
            if payload.get("cleanup_confirmed") is not True:
                return 409, {"error": "cleanup_not_confirmed"}
            CLAIM["phase"] = "released"
        return 200, _snapshot_locked(private=True)

    def do_GET(self):
        path = self.path.split("?")[0]
        if not self._local_request():
            return self._json(403, {"error": "foreign_origin"})
        if path == "/__state":
            with LOCK:
                value = _snapshot_locked(private=False)
            return self._json(200, value)
        with LOCK:
            binding = self._browser_binding_locked()
            if binding is None:
                body = None
            else:
                body = render(path)
                if body is not None:
                    _append_locked("GET", path, binding)
        if binding is None:
            return self._json(403, {"error": "browser_owner_mismatch"})
        return self._send(200, body) if body else self._send(404, page("Not found", ""))

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._local_request():
            return self._json(403, {"error": "foreign_origin"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= MAX_BODY_BYTES:
                raise ValueError("body_size")
            raw = self.rfile.read(length).decode("utf-8")
        except (ValueError, UnicodeError):
            return self._json(400, {"error": "invalid_body"})
        if path == "/__claim" or path.startswith("/__owner/"):
            try:
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("content_type")
                payload = json.loads(raw)
                if type(payload) is not dict:
                    raise ValueError("payload_type")
            except ValueError:
                return self._json(400, {"error": "invalid_control_request"})
            with LOCK:
                code, value = self._control_locked(path, payload)
            return self._json(code, value)
        data = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        with LOCK:
            binding = self._browser_binding_locked()
            if binding is None:
                code = 403
            elif path == "/settings":
                STATE["notify_email"] = data.get("notify_email") == "on"
                STATE["settings_saved"] += 1
                code = 303
            elif path == "/form":
                STATE["form"] = {"name": data.get("name", ""), "email": data.get("email", "")}
                code = 303
            elif path == "/danger/delete":
                STATE["account_deleted"] = True
                code = 303
            elif path == "/slow/export":
                pending_id = str(uuid.uuid4())
                PENDING_EFFECTS[pending_id] = {"path": path, "binding": copy.deepcopy(binding)}
                code = 202
            else:
                code = 404
            if code == 303:
                _append_locked("POST", path, binding)
        if code == 403:
            return self._json(403, {"error": "browser_owner_mismatch"})
        if code == 404:
            return self._send(404, page("Not found", ""))
        if code == 202:
            time.sleep(self.slow_seconds)
            with LOCK:
                PENDING_EFFECTS.pop(pending_id, None)
                still_owned = (CLAIM is not None and CLAIM["phase"] == "active" and
                               CLAIM["binding"] == binding)
                if still_owned:
                    STATE["exports"] += 1
                    _append_locked("POST", path, binding)
            if not still_owned:
                return self._json(409, {"error": "claim_fenced"})
        self._redirect(path.rsplit("/", 1)[0] or "/" if path.count("/") > 1 else path)

    def log_message(self, *args):
        pass


def main() -> None:
    global RUN_ID
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--slow-seconds", type=float, default=3.0)
    args = parser.parse_args()
    RUN_ID = args.run_id
    Handler.slow_seconds = args.slow_seconds
    reset()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
