"""A small order-desk site for V0.6 browser checks, with NO ReflexMesh markup.

    python server.py --port 8770

Plain HTML in an ordinary shape: a nav, a search form, a results list, an order page with
a note form. Nothing here names a ReflexMesh ID, and the core has no scenario for it. The
state oracle at /__state is for tests that must check what the server recorded; the
control loop never reads it.

Routes:
  /                    dashboard
  /leave               redirects to another origin (localhost), for allowed-origins checks
  /orders[?q=...]      search form, and matching orders when q is given
  /orders/<id>         one order: details, saved notes, a note form with priority and notify
  /orders/<id>/tracking    a small page, opened by the order page in a new tab
  POST /orders/<id>/note   saves a note (303 back to the order)
  /__state             JSON: recorded requests and notes
"""
import argparse
import json
import threading
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
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlsplit(self.path)
        with LOCK:
            REQUESTS.append({"method": "GET", "path": parts.path, "host": self.headers.get("Host")})
        if parts.path == "/__state":
            with LOCK:
                data = json.dumps({"notes": NOTES, "meta": META, "requests": REQUESTS}, ensure_ascii=False).encode()
            return self._send(200, data, "application/json")
        if parts.path == "/":
            return self._send(200, dashboard(self.server.server_port))
        if parts.path == "/leave":  # a same-origin link that the server turns into another origin
            return self._send(302, b"", location=f"http://localhost:{self.server.server_port}/")
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
        pieces = parts.path.strip("/").split("/")
        length = min(int(self.headers.get("Content-Length") or 0), 65536)
        fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        with LOCK:
            REQUESTS.append({"method": "POST", "path": parts.path, "host": self.headers.get("Host")})
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
