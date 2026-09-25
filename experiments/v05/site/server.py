"""V0.5 isolated test site: launch one process per attempt with --run-id.

    python server.py --port 8765

GET /__state returns a run ID, sequence, log, and state. /slow/export is delayed.
"""
import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import parse_qs

LOCK = threading.Lock()
STATE: dict = {}
LOG: list = []
RUN_ID = ""
SEQUENCE = 0


def reset() -> None:
    global SEQUENCE
    with LOCK:
        STATE.clear()
        STATE.update({"notify_email": False, "settings_saved": 0, "form": None, "account_deleted": False,
                      "exports": 0})
        LOG.clear()
        SEQUENCE = 0


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

    def _log(self, method: str, data: dict | None = None) -> None:
        global SEQUENCE
        with LOCK:
            SEQUENCE += 1
            LOG.append({"seq": SEQUENCE, "t": time.time(), "method": method,
                        "path": self.path.split("?")[0], "data": data})

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to: str) -> None:
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/__state":
            with LOCK:
                return self._send(200, json.dumps({"run_id": RUN_ID, "sequence": SEQUENCE,
                                                   "log": LOG, "state": STATE}).encode(), "application/json")
        if path == "/favicon.ico":
            return self._send(404, b"")
        self._log("GET")
        body = render(path)
        return self._send(200, body) if body else self._send(404, page("Not found", ""))

    def do_POST(self):
        path = self.path.split("?")[0]
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        data = {k: v[0] for k, v in parse_qs(raw).items()}
        self._log("POST", data)
        with LOCK:
            if path == "/settings":
                STATE["notify_email"] = data.get("notify_email") == "on"
                STATE["settings_saved"] += 1
            elif path == "/form":
                STATE["form"] = {"name": data.get("name", ""), "email": data.get("email", "")}
            elif path == "/danger/delete":
                STATE["account_deleted"] = True
            elif path == "/slow/export":
                pass
            else:
                return self._send(404, page("Not found", ""))
        if path == "/slow/export":
            time.sleep(self.slow_seconds)
            with LOCK:
                STATE["exports"] += 1
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
