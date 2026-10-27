"""The session service (`reflexmesh browser serve`): a local socket around BrowserCore."""

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reflexmesh.runtime.browser_service import WINDOWS, SessionService, call  # noqa: E402

CLI = [sys.executable, "-c", "import sys; from reflexmesh.cli import main; sys.exit(main())"]


class FakeCore:
    def __init__(self):
        self.chains, self.running, self.calls, self.closed = {}, {}, [], False

    def execute(self, task):
        self.calls.append(("execute", task.task_id))
        return {"attempt_status": "incomplete", "task_id": task.task_id}

    def resume(self, continuation):
        self.calls.append(("resume", continuation))
        return {"schema_version": "browser-error/0.1", "error": {"code": "token_invalid"}}

    def cancel(self, chain_id):
        self.calls.append(("cancel", chain_id))
        return {"chain_id": chain_id, "accepted": False}

    def close(self):
        self.closed = True


class Protocol(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "s.sock"
        self.core = FakeCore()
        self.service = SessionService(self.core, self.path)
        self.service.start()
        self.thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.service.shutdown()
        self.thread.join(5)

    def test_the_socket_is_private(self):
        if not WINDOWS:
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            return
        acl = subprocess.run(["icacls", str(self.path)], capture_output=True, text=True).stdout
        grants = [line for line in acl.splitlines() if ":(" in line]
        self.assertEqual(len(grants), 1, acl)                 # the current user alone, nothing inherited
        self.assertIn(os.environ["USERNAME"].lower(), grants[0].lower())
        self.assertIn("(F)", grants[0])

    def test_requests_reach_the_core_and_bad_ones_do_not(self):
        from v06_samples import task_dict

        self.assertEqual(call(self.path, {"op": "execute", "task": task_dict()})["attempt_status"], "incomplete")
        self.assertEqual(call(self.path, {"op": "execute", "task": {"x": 1}})["error"]["code"], "invalid_task")
        self.assertEqual(call(self.path, {"op": "resume", "continuation": {"token": "t"}})["error"]["code"],
                         "token_invalid")
        self.assertEqual(call(self.path, {"op": "nope"})["error"]["code"], "unknown_op")
        self.assertEqual(call(self.path, {"op": "status"})["chains"], [])
        self.assertEqual(self.raw(b"not json\n")["error"]["code"], "invalid_request")
        self.assertEqual([c[0] for c in self.core.calls], ["execute", "resume"])

    def raw(self, data: bytes) -> dict:
        if WINDOWS:
            from multiprocessing.connection import Client

            address = json.loads(self.path.read_text())
            with Client(address["pipe"], family="AF_PIPE", authkey=bytes.fromhex(address["key"])) as conn:
                conn.send_bytes(data)
                return json.loads(conn.recv_bytes())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.connect(str(self.path))
            raw.sendall(data)
            return json.loads(raw.makefile().readline())

    def test_a_caller_without_the_key_gets_nothing(self):
        if not WINDOWS:
            self.skipTest("the Unix socket checks the peer's user instead (SO_PEERCRED)")
        from multiprocessing import AuthenticationError
        from multiprocessing.connection import Client

        address = json.loads(self.path.read_text())
        with self.assertRaises((AuthenticationError, EOFError, OSError)):
            with Client(address["pipe"], family="AF_PIPE", authkey=b"wrong" * 8) as conn:
                conn.send_bytes(b'{"op": "status"}')
                conn.recv_bytes()
        self.assertEqual(call(self.path, {"op": "status"})["chains"], [])     # and the service is still there

    def test_a_second_service_on_a_live_socket_is_refused_and_a_stale_one_is_replaced(self):
        with self.assertRaises(FileExistsError):
            SessionService(FakeCore(), self.path).start()
        stale = Path(self.dir.name) / "stale.sock"
        if WINDOWS:                                    # the address of a pipe that no longer exists
            stale.write_text(json.dumps({"schema_version": "browser-service/0.1", "key": "00",
                                         "pipe": "\\\\.\\pipe\\reflexmesh-browser-gone"}))
        else:
            dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            dead.bind(str(stale))
            dead.close()                               # a socket file nobody listens on
        other = SessionService(FakeCore(), stale)
        other.start()
        if WINDOWS:
            other.listener.close()
        else:
            other.server.server_close()
        plain = Path(self.dir.name) / "file"
        plain.write_text("x")
        with self.assertRaises(FileExistsError):
            SessionService(FakeCore(), plain).start()  # never deletes a file that is not a socket

    def test_shutdown_closes_the_core_and_removes_the_socket(self):
        self.assertTrue(call(self.path, {"op": "shutdown"})["shutting_down"])
        self.thread.join(5)
        self.assertTrue(self.core.closed)
        self.assertFalse(self.path.exists())


from test_v06_browser_real import AVAILABLE, CHROME, SANDBOX, SITE, BrowserCase  # noqa: E402


@unittest.skipUnless(AVAILABLE, "set REFLEXMESH_CHROME and install the pinned Browser Use + SystemOneHarness")
class OverTheSocket(BrowserCase):
    """The CLI end to end: `serve` in a process of its own, `submit` and `resume` as separate one-shot processes."""

    def task(self, *, revision=1, without_order=False):
        data = json.loads((ROOT / "experiments/v06/tasks/order-note-verified.json").read_text("utf-8"))
        data.update(start_url=self.origin + "/orders", allowed_origins=[self.origin], revision=revision)
        if without_order:
            data["text_slots"] = [s for s in data["text_slots"] if s["id"] != "order"]
        return data

    def serve(self, scripts):
        folder = Path(self.out.name)
        (folder / "script.json").write_text(json.dumps(scripts))
        (folder / "checks.json").write_text(json.dumps(
            {"checks": {"order_desk.note_saved_once": {"kind": "order_desk", "url": self.origin}}}))
        self.sock = folder / "svc.sock"
        env = {**os.environ, "REFLEXMESH_CHROME": CHROME, "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
        if not SANDBOX:
            env["REFLEXMESH_CHROME_NO_SANDBOX"] = "1"
        self.proc = subprocess.Popen([*CLI, "browser", "serve", "--socket",
                                      str(self.sock), "--state-dir", str(folder / "state"), "--action-provider",
                                      "script", "--script", str(folder / "script.json"), "--checks",
                                      str(folder / "checks.json")], env={**env, "PYTHONPATH": str(ROOT / "src")},
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self.stop_service)
        self.assertIn("listening", self.proc.stdout.readline())

    def stop_service(self):
        if self.proc.poll() is None:
            self.cli("shutdown")
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def cli(self, command, payload=None, *extra):
        done = subprocess.run([*CLI, "browser", command, "--socket", str(self.sock),
                               *(["--input", "-"] if payload is not None else []), *extra],
                              input=json.dumps(payload) if payload is not None else None, capture_output=True,
                              text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, timeout=180)
        return done.returncode, json.loads(done.stdout or "{}")

    def test_submit_then_resume_in_the_same_browser_from_separate_processes(self):
        to_order = [{"action": "type_text", "field": "Order number", "value": "^order@1"},
                    {"action": "click", "element": "button 'Search'"}, {"action": "click", "element": "Заказ №125"},
                    {"action": "type_text", "field": "textbox 'Note'", "value": "^note@1"},
                    {"action": "click", "element": "Save note"}, {"action": "finish"}]
        self.serve([[{"action": "escalate"}], to_order])
        code, first = self.cli("submit", self.task(without_order=True))
        self.assertEqual((code, first.get("attempt_status")), (4, "incomplete"), first)
        cont = first["handoff"]["continuation"]
        self.assertTrue(cont["allowed"])                          # the service holds the session: resume is offered
        code, status = self.cli("status")
        self.assertEqual(status["chains"][0]["session_open"], True)
        code, second = self.cli("resume", {"token": cont["token"], "parent_attempt_id": first["attempt_id"],
                                           "operation": "start", "mode": "jev", "task": self.task(revision=2)})
        self.assertEqual((code, second["attempt_status"], second["stop_reason"]), (0, "completed", "verified"))
        self.assertEqual(second["session_id"], first["session_id"])
        code, again = self.cli("resume", {"token": cont["token"], "parent_attempt_id": first["attempt_id"],
                                          "operation": "start", "mode": "jev"})
        self.assertEqual((code, again["error"]["code"]), (2, "token_invalid"))
        self.assertEqual(SITE.NOTES, {"125": ["Перезвонить завтра"]})
        stored = next((Path(self.out.name) / "state").glob("chain-*/attempt-1/result.json"))
        self.assertNotIn(cont["token"], stored.read_text())      # the token went to the caller, not to the disk

    def test_cancel_reaches_a_running_attempt(self):
        loop = [{"action": "click", "element": "Dashboard"}, {"action": "click", "element": "link 'Orders'"}] * 30
        self.serve([loop])
        box = {}
        worker = threading.Thread(target=lambda: box.update(result=self.cli("submit", self.task())))
        worker.start()
        chain_id = None
        for _ in range(100):
            chains = self.cli("status")[1].get("chains") or []
            if chains and chains[0]["running"]:
                chain_id = chains[0]["chain_id"]
                break
            time.sleep(0.2)
        self.assertIsNotNone(chain_id)
        time.sleep(1.0)
        self.assertTrue(self.cli("cancel", None, "--chain-id", chain_id)[1]["accepted"])
        worker.join(60)
        code, result = box["result"]
        self.assertEqual((code, result["attempt_status"], result["stop_reason"]), (130, "cancelled", "cancel_requested"))


if __name__ == "__main__":
    unittest.main()
