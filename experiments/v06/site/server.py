"""A small order-desk site for V0.6 browser checks, with NO ReflexMesh markup.

    python server.py --port 8770

Plain HTML in an ordinary shape: a nav, a search form, a results list, an order page with
a note form. Nothing here names a ReflexMesh ID, and the core has no scenario for it. The
state oracle at /__state is for tests that must check what the server recorded; the
control loop never reads it.

Routes:
  /                    dashboard
  /catalog             260 buttons, to see how a dense page is reported
  /leave               redirects to another origin (localhost), for allowed-origins checks
  /orders[?q=...]      search form, and matching orders when q is given
  /orders/<id>         one order: details, saved notes, a note form with priority and notify
  /orders/<id>/tracking    a small page, opened by the order page in a new tab
  POST /orders/<id>/note   saves a note (303 back to the order)
  /__state             JSON: recorded requests and notes
  POST /__claim        claim the site for one attempt: {"attempt_id"} -> {"claim_id", "token"}; 409 while claimed
  GET /__evidence      with `Authorization: Bearer <token>`: what happened under the claim (requests, notes
                       added, requests still being handled); the registered check `order_desk` reads this
  POST /__release      with the token: end the claim

The claim is what binds the server's record to one attempt: every request is tagged with the claim that was
active when it arrived, and a second claim is refused while one is active. It cannot tell a browser driven by
ReflexMesh from any other client that reaches the site during the claim; the site is local and started for runs.
"""
import argparse
import hashlib
import hmac
import json
import secrets
import threading
import uuid
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

LOCK = threading.Lock()
ORDERS = {
    "124": {"customer": "Ivanov", "status": "Shipped"},
    "125": {"customer": "Petrov", "status": "Processing"},
    "126": {"customer": "Sidorova", "status": "Delivered"},
}
NOTES: dict[str, list[str]] = {}
META: dict[str, dict] = {}
REQUESTS: list[dict] = []
CLAIM: dict = {}          # claim_id, attempt_id, token_hash, notes_at
IN_FLIGHT = [0]           # page requests being handled right now


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>{escape(title)}</title>
<style>body{{font:16px sans-serif;margin:2rem}} nav a{{margin-right:1rem}} li{{margin:.4rem 0}}
label{{display:block;margin:.6rem 0 .2rem}} textarea{{width:24rem;height:5rem}}</style></head>
<body><nav><a href="/">Dashboard</a><a href="/orders">Orders</a></nav>
<h1>{escape(title)}</h1>{body}</body></html>""".encode("utf-8")


def dashboard(port: int) -> bytes:
    # A link to another origin (localhost, not 127.0.0.1) for the allowed-origins checks.
    return page("Dashboard", "<p>Welcome. Use <b>Orders</b> to look up an order.</p>"
                f'<p><a href="http://localhost:{port}/">External portal</a></p>'
                '<p><a href="/leave">Partner offer</a></p>')


def catalog() -> bytes:
    """A dense page: 260 buttons, far more than one observation may list."""
    buttons = "".join(f'<li><button type="button">Item {n}</button></li>' for n in range(1, 261))
    return page("Catalog", f"<ul>{buttons}</ul>")


def orders(query: str) -> bytes:
    form = ('<form method="get" action="/orders"><label for="q">Order number</label>'
            f'<input id="q" name="q" type="text" value="{escape(query)}">'
            '<button type="submit">Search</button></form>')
    result = ""
    if query:
        found = [oid for oid in sorted(ORDERS) if query in oid]
        if found:
            items = "".join(f'<li><a href="/orders/{oid}">Заказ №{oid}</a> — {escape(ORDERS[oid]["status"])}</li>'
                            for oid in found)
            result = f"<h2>Results</h2><ul>{items}</ul>"
        else:
            result = "<h2>Results</h2><p>No orders found.</p>"
    return page("Orders", form + result)


def order(oid: str) -> bytes | None:
    if oid not in ORDERS:
        return None
    info = ORDERS[oid]
    with LOCK:
        notes = list(NOTES.get(oid, []))
    shown = "".join(f"<li>{escape(n)}</li>" for n in notes) or "<li>No notes yet.</li>"
    body = (f"<p>Customer: {escape(info['customer'])}. Status: {escape(info['status'])}.</p>"
            f"<h2>Notes</h2><ul>{shown}</ul>"
            f'<p><a href="/orders/{oid}/tracking" target="_blank">Open tracking</a></p>'
            f'<form method="post" action="/orders/{oid}/note"><label for="note">Note</label>'
            '<textarea id="note" name="note"></textarea>'
            '<label for="priority">Priority</label>'
            '<select id="priority" name="priority"><option>Normal</option><option>High</option>'
            '<option>Low</option></select>'
            '<label><input type="checkbox" name="notify"> Notify customer by email</label>'
            '<button type="submit">Save note</button></form>')
    return page(f"Заказ №{oid}", body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8", location: str | None = None):
        # The state change is done once a response is ready: stop counting the request before the client can
        # see the answer, or a client that asks for evidence right after it would still find it pending.
        self._done()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def _record(self, method: str, path: str) -> None:
        self._counted = not path.startswith("/__")
        with LOCK:
            REQUESTS.append({"method": method, "path": path, "host": self.headers.get("Host"),
                             "claim": CLAIM.get("claim_id")})
            if self._counted:
                IN_FLIGHT[0] += 1

    def _done(self, *_) -> None:
        if getattr(self, "_counted", False):
            self._counted = False
            with LOCK:
                IN_FLIGHT[0] -= 1

    def _authorized(self) -> bool:
        given = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        with LOCK:
            expected = CLAIM.get("token_hash")
        return bool(expected and given) and hmac.compare_digest(hashlib.sha256(given.encode()).hexdigest(), expected)

    def _json(self, code: int, value) -> None:
        self._send(code, json.dumps(value, ensure_ascii=False).encode(), "application/json")

    def do_GET(self):
        parts = urlsplit(self.path)
        self._record("GET", parts.path)
        try:
            self._get(parts)
        finally:
            self._done(parts.path)

    def _get(self, parts):
        if parts.path == "/__evidence":
            if not self._authorized():
                return self._json(401, {"error": "not_authorized"})
            with LOCK:
                claim = CLAIM.get("claim_id")
                data = {"claim_id": claim, "attempt_id": CLAIM.get("attempt_id"),
                        "requests": [{"method": r["method"], "path": r["path"]} for r in REQUESTS
                                     if r.get("claim") == claim and not r["path"].startswith("/__")],
                        "notes_added": {oid: list(notes[CLAIM["notes_at"].get(oid, 0):])
                                        for oid, notes in NOTES.items() if len(notes) > CLAIM["notes_at"].get(oid, 0)},
                        "pending": IN_FLIGHT[0]}
            return self._json(200, data)
        if parts.path == "/__state":
            with LOCK:
                data = json.dumps({"notes": NOTES, "meta": META, "requests": REQUESTS}, ensure_ascii=False).encode()
            return self._send(200, data, "application/json")
        if parts.path == "/":
            return self._send(200, dashboard(self.server.server_port))
        if parts.path == "/leave":  # a same-origin link that the server turns into another origin
            return self._send(302, b"", location=f"http://localhost:{self.server.server_port}/")
        if parts.path == "/catalog":
            return self._send(200, catalog())
        if parts.path == "/orders":
            return self._send(200, orders(parse_qs(parts.query).get("q", [""])[0].strip()))
        if parts.path.startswith("/orders/"):
            pieces = parts.path.split("/")
            if len(pieces) == 4 and pieces[3] == "tracking" and pieces[2] in ORDERS:
                return self._send(200, page("Tracking", f"<p>Parcel for order {pieces[2]} is on its way.</p>"))
            body = order(pieces[2])
            if body is not None and len(pieces) == 3:
                return self._send(200, body)
        self._send(404, page("Not found", "<p>No such page.</p>"))

    def do_POST(self):
        parts = urlsplit(self.path)
        self._record("POST", parts.path)
        try:
            self._post(parts)
        finally:
            self._done(parts.path)

    def _post(self, parts):
        pieces = parts.path.strip("/").split("/")
        length = min(int(self.headers.get("Content-Length") or 0), 65536)
        raw = self.rfile.read(length)
        if parts.path == "/__claim":
            try:
                attempt = str(json.loads(raw or b"{}").get("attempt_id") or "")
            except (ValueError, AttributeError):
                attempt = ""
            if not attempt:
                return self._json(400, {"error": "attempt_id_required"})
            token = secrets.token_urlsafe(32)
            with LOCK:
                if CLAIM:
                    return self._json(409, {"error": "claimed"})
                CLAIM.update(claim_id=uuid.uuid4().hex, attempt_id=attempt,
                             token_hash=hashlib.sha256(token.encode()).hexdigest(),
                             notes_at={oid: len(notes) for oid, notes in NOTES.items()})
                claim = CLAIM["claim_id"]
            return self._json(200, {"claim_id": claim, "attempt_id": attempt, "token": token})
        if parts.path == "/__release":
            if not self._authorized():
                return self._json(401, {"error": "not_authorized"})
            with LOCK:
                CLAIM.clear()
            return self._json(200, {"released": True})
        fields = parse_qs(raw.decode("utf-8", "replace"))
        if len(pieces) == 3 and pieces[0] == "orders" and pieces[2] == "note" and pieces[1] in ORDERS:
            note = fields.get("note", [""])[0].strip()
            if note:
                with LOCK:
                    NOTES.setdefault(pieces[1], []).append(note)
                    META[pieces[1]] = {"priority": fields.get("priority", [""])[0],
                                       "notify": "notify" in fields}
            return self._send(303, b"", location=f"/orders/{pieces[1]}")
        self._send(404, page("Not found", "<p>No such page.</p>"))


def serve(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
