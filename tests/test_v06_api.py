"""V0.6 Task API, MCP server, handoff and continuation (U level; no browser needed)."""

import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reflexmesh.api.service import SHUTDOWN_BOUND, ApiError, TaskService
from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.handoff import build_handoff
from reflexmesh.verification.verifier import FixtureVerifier, read_fixture
from test_v05_runtime import sample

ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
NO_CHROME = "/nonexistent/reflexmesh-test-chrome"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Silent:
    """A loopback listener that accepts connections and never answers (a hung fixture)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]

    def close(self):
        self.sock.close()


def task_for(port, wall=10, run_id="api-run"):
    data = sample()
    data["fixture"] = {"origin": f"http://127.0.0.1:{port}", "run_id": run_id}
    data["limits"]["wall_seconds"] = wall
    data["limits"]["max_steps"] = 8
    data["limits"]["max_model_calls"] = 8
    return data


class McpClient:
    def __init__(self, state_dir, *extra):
        self.process = subprocess.Popen([sys.executable, "-m", "reflexmesh", "mcp", "--state-dir", str(state_dir),
                                         "--chrome", NO_CHROME, *extra],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=ENV, text=True, bufsize=1)
        self.next_id = 0
        self.lock = threading.Lock()
        self.pending = {}
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            message = json.loads(line)
            with self.lock:
                self.pending[message.get("id")] = message

    def raw(self, line):
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def request(self, method, params=None, timeout=60):
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
        self.raw(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                             **({"params": params} if params is not None else {})}))
        return self.wait(request_id, timeout)

    def wait(self, request_id, timeout=60):
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            with self.lock:
                if request_id in self.pending:
                    return self.pending.pop(request_id)
            time.sleep(0.01)
        raise TimeoutError(request_id)

    def tool(self, name, **arguments):
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        result = response["result"]
        self.assert_consistent(result)
        return result["structuredContent"], result["isError"]

    @staticmethod
    def assert_consistent(result):
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]

    def initialize(self):
        response = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "test", "version": "0"}})
        self.raw(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        return response

    def close(self, timeout=30):
        self.process.stdin.close()
        try:
            return self.process.wait(timeout=timeout)
        finally:
            if self.process.poll() is None:
                self.process.kill()


class McpProtocol(unittest.TestCase):
    """A01 and A05."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = McpClient(Path(self.tmp.name) / "state")
        self.silent = Silent()

    def tearDown(self):
        self.client.close()
        self.silent.close()
        self.tmp.cleanup()

    def test_handshake_and_tools(self):
        init = self.client.initialize()["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertEqual(init["serverInfo"]["name"], "reflexmesh")
        self.assertIn("tools", init["capabilities"])
        self.assertEqual(self.client.request("ping")["result"], {})
        tools = {t["name"]: t for t in self.client.request("tools/list")["result"]["tools"]}
        self.assertEqual(set(tools), {"reflexmesh_submit", "reflexmesh_status", "reflexmesh_cancel",
                                      "reflexmesh_continue"})
        self.assertTrue(tools["reflexmesh_status"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["reflexmesh_submit"]["annotations"]["readOnlyHint"])
        unknown = self.client.request("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(unknown["protocolVersion"], "2025-11-25")

    def test_protocol_and_input_errors_start_nothing(self):
        self.client.initialize()
        self.client.raw("{not json")
        self.assertEqual(self.client.wait(None)["error"]["code"], -32700)
        self.assertEqual(self.client.request("resources/subscribe")["error"]["code"], -32601)
        bad_task = task_for(self.silent.port)
        bad_task["limits"]["max_action_retries"] = 1
        cases = [
            ("reflexmesh_submit", {"task": bad_task, "routing_provider": "stub", "action_provider": "script",
                                   "script": [{"action": "finish"}]}, "invalid_input"),
            ("reflexmesh_submit", {"task": task_for(self.silent.port), "routing_provider": "stub",
                                   "action_provider": "script"}, "invalid_input"),
            ("reflexmesh_submit", {"task": task_for(self.silent.port), "routing_provider": "stub",
                                   "action_provider": "script", "script": [{"action": "finish"}], "extra": 1},
             "invalid_input"),
            ("reflexmesh_status", {"attempt_id": "0" * 32}, "unknown_attempt"),
            ("reflexmesh_status", {"attempt_id": "../../etc"}, "unknown_attempt"),
            ("reflexmesh_cancel", {"attempt_id": "f" * 32}, "unknown_attempt"),
            ("reflexmesh_frobnicate", {}, "unknown_tool"),
        ]
        for name, arguments, code in cases:
            with self.subTest(name=name, code=code):
                payload, is_error = self.client.tool(name, **arguments)
                self.assertTrue(is_error)
                self.assertEqual(payload["error"]["code"], code)
        attempts = Path(self.tmp.name) / "state" / "attempts"
        self.assertEqual(list(attempts.iterdir()), [])

    def test_busy_then_cancel_and_handoff(self):
        self.client.initialize()
        arguments = {"task": task_for(self.silent.port, wall=20), "routing_provider": "stub",
                     "action_provider": "script", "script": [{"action": "finish"}]}
        first, is_error = self.client.tool("reflexmesh_submit", **arguments)
        self.assertFalse(is_error, first)
        busy, is_error = self.client.tool("reflexmesh_submit", **arguments)
        self.assertEqual((is_error, busy["error"]["code"]), (True, "busy"))
        running, _ = self.client.tool("reflexmesh_status", attempt_id=first["attempt_id"])
        self.assertEqual(running["state"], "running")
        time.sleep(0.5)
        cancel, _ = self.client.tool("reflexmesh_cancel", attempt_id=first["attempt_id"])
        self.assertEqual(cancel["cancel"], "requested")
        done, _ = self.client.tool("reflexmesh_status", attempt_id=first["attempt_id"], wait_seconds=20)
        self.assertEqual(done["state"], "terminal", done)
        result = done["result"]
        self.assertEqual((result["attempt_status"], result["stop_reason"]), ("cancelled", "cancel_requested"))
        self.assertEqual(result["attempt_id"], first["attempt_id"])
        handoff = result["handoff"]
        self.assertEqual((handoff["kind"], handoff["attempted"], handoff["continuation"]["allowed"]),
                         ("cancelled", False, True))
        again, _ = self.client.tool("reflexmesh_cancel", attempt_id=first["attempt_id"])
        self.assertEqual(again["cancel"], "already_terminal")

    def test_shutdown_cancels_and_reaps_running_attempt(self):
        self.client.initialize()
        submitted, _ = self.client.tool("reflexmesh_submit", task=task_for(self.silent.port, wall=60),
                                        routing_provider="stub", action_provider="script",
                                        script=[{"action": "finish"}])
        time.sleep(0.5)
        started = time.monotonic()
        self.assertEqual(self.client.close(timeout=SHUTDOWN_BOUND + 5), 0)
        self.assertLess(time.monotonic() - started, SHUTDOWN_BOUND + 3)
        result = json.loads((Path(self.tmp.name) / "state" / "attempts" / submitted["attempt_id"] / "out" /
                             "result.json").read_text())
        self.assertEqual(result["attempt_status"], "cancelled")


class SameSemantics(unittest.TestCase):
    """A02: CLI and MCP give the same result for the same task."""

    def test_cli_and_mcp_agree_on_blocked_task(self):
        port = free_port()  # Nothing listens: the fixture preflight blocks.
        task = task_for(port)
        keys = ("schema_version", "attempt_status", "stop_reason", "task_outcome", "execution_outcome",
                "executor_id", "cleanup")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.json"
            path.write_text(json.dumps(task))
            (Path(tmp) / "script.json").write_text('[{"action":"finish"}]')
            cli = subprocess.run([sys.executable, "-m", "reflexmesh", "run", "--input", str(path), "--output-dir",
                                  str(Path(tmp) / "out"), "--routing-provider", "stub", "--action-provider",
                                  "script", "--script", str(Path(tmp) / "script.json"), "--chrome", NO_CHROME],
                                 env=ENV, capture_output=True, timeout=30)
            cli_result = json.loads(cli.stdout)
            client = McpClient(Path(tmp) / "state")
            try:
                client.initialize()
                submitted, _ = client.tool("reflexmesh_submit", task=task, routing_provider="stub",
                                           action_provider="script", script=[{"action": "finish"}])
                done, _ = client.tool("reflexmesh_status", attempt_id=submitted["attempt_id"], wait_seconds=30,
                                      detail="full")
            finally:
                client.close()
        mcp_result = done["result"]
        self.assertEqual(cli.returncode, 3)
        self.assertEqual({k: cli_result[k] for k in keys}, {k: mcp_result[k] for k in keys})
        self.assertEqual(cli_result["stop_reason"], "executor_unavailable")
        self.assertEqual([(r["id"], r["status"]) for r in cli_result["verification"]],
                         [(r["id"], r["status"]) for r in mcp_result["verification"]])
        self.assertEqual(cli_result["handoff"]["kind"], mcp_result["handoff"]["kind"])
        self.assertEqual(cli_result["handoff"]["kind"], "blocked")


def synthetic_result(task, *, status="incomplete", reason="no_confident_action", actions=(), gaps=None,
                     used=None, attempt_id="a" * 32):
    trace = [{"seq": 1, "kind": "observation", "data": [{"url": task.origin + task.start_path,
                                                          "content_gaps": gaps or []}]}]
    return {"schema_version": "execution-result/0.2", "attempt_id": attempt_id, "executor_id": "browser.soh",
            "attempt_status": status, "stop_reason": reason, "task_outcome": "unknown",
            "actions": list(actions), "verification": [{"id": "sent", "kind": "postcondition", "status": "unknown"}],
            "trace": trace, "trace_ref": "trace.jsonl", "evidence_refs": [],
            "chain": {"root_attempt_id": attempt_id, "parent_attempt_id": None, "sequence": 1,
                      "used": used or {"steps": 2, "model_calls": 1, "wall_seconds": 3.5}},
            "budget": {"steps": 2, "model_calls": 1, "elapsed_seconds": 3.5}}


class Handoff(unittest.TestCase):
    """A03 and the content-gap case."""

    def setUp(self):
        self.task = ExecutionTask.from_dict(task_for(free_port()))
        self.limits = {"wall_seconds": 10, "max_steps": 8, "max_model_calls": 8, "max_action_retries": 0}

    def build(self, result):
        return build_handoff(self.task, result, root_limits=self.limits, usage=result["chain"]["used"])

    def test_completed_has_no_handoff(self):
        self.assertIsNone(self.build(synthetic_result(self.task, status="completed", reason="verified")))

    def test_outcomes_distinguish_done_not_done_possibly_done(self):
        actions = [{"id": 1, "operation": "navigate", "target_id": "bootstrap", "effect": "applied"},
                   {"id": 2, "operation": "type_text", "target_id": "name", "slot_ref": "name@1", "effect": "applied"},
                   {"id": 3, "operation": "submit_form", "target_id": "send-form", "effect": "not_applied"}]
        handoff = self.build(synthetic_result(self.task, actions=actions))
        self.assertEqual([a["outcome"] for a in handoff["actions"]], ["done", "done", "not_done"])
        self.assertEqual((handoff["kind"], handoff["attempted"]), ("incomplete", True))
        self.assertEqual(handoff["remaining_budget"], {"steps": 6, "model_calls": 7, "wall_seconds": 6.5})
        self.assertEqual(handoff["constraints"]["text_slot_refs"], ["name@1"])
        self.assertNotIn("secret-text", json.dumps(handoff))

    def test_unknown_effect_refuses_continuation(self):
        actions = [{"id": 2, "operation": "submit_form", "target_id": "send-form", "effect": "unknown"}]
        handoff = self.build(synthetic_result(self.task, actions=actions, gaps=["support-message"]))
        self.assertEqual((handoff["kind"], handoff["continuation"]),
                         ("unknown_effect", {"allowed": False, "reason": "unresolved_effect"}))
        self.assertEqual(handoff["actions"][0]["outcome"], "possibly_done")

    def test_content_gap_yields_needs_content(self):
        handoff = self.build(synthetic_result(self.task, status="failed", reason="postcondition_failed",
                                              gaps=["support-message"]))
        self.assertEqual(handoff["kind"], "needs_content")
        self.assertEqual(handoff["needs"], [{"kind": "content", "target_id": "support-message",
                                             "reason": "required_field_empty_without_slot"}])

    def test_exhausted_budget_refuses_continuation(self):
        used = {"steps": 8, "model_calls": 1, "wall_seconds": 3}
        handoff = self.build(synthetic_result(self.task, used=used))
        self.assertEqual(handoff["continuation"], {"allowed": False, "reason": "budget_exhausted"})


class Continuation(unittest.TestCase):
    """A04, using recorded parent attempts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = TaskService(Path(self.tmp.name) / "state", chrome=NO_CHROME, env=ENV)
        self.port = free_port()

    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()

    def parent(self, *, actions=(), status="failed", reason="postcondition_failed", gaps=("support-message",),
               used=None, attempt_id="b" * 32, chain=None):
        data = task_for(self.port, wall=30)
        directory = self.service.root / "attempts" / attempt_id
        (directory / "out").mkdir(parents=True)
        (directory / "task.json").write_text(json.dumps(data))
        (directory / "request.json").write_text(json.dumps({"attempt_id": attempt_id, "submitted_at": time.time()}))
        task = ExecutionTask.from_dict(data)
        result = synthetic_result(task, status=status, reason=reason, actions=actions, gaps=list(gaps),
                                  used=used or {"steps": 3, "model_calls": 2, "wall_seconds": 10.0},
                                  attempt_id=attempt_id)
        if chain:
            result["chain"].update(chain)
        result["handoff"] = build_handoff(task, result, root_limits=data["limits"], usage=result["chain"]["used"])
        (directory / "out" / "result.json").write_text(json.dumps(result))
        return attempt_id, data

    def child_task(self, started):
        directory = self.service.root / "attempts" / started["attempt_id"]
        return json.loads((directory / "task.json").read_text()), json.loads((directory / "chain.json").read_text())

    def test_new_slot_creates_revision_with_remaining_budget(self):
        parent_id, data = self.parent()
        slot = {"id": "message", "version": 1, "value": "Please call me back."}
        started = self.service.continue_task(parent_id, {"add_text_slots": [slot]}, "stub", "script",
                                             [{"action": "finish"}])
        child, chain = self.child_task(started)
        self.assertEqual((child["revision"], child["task_id"]), (data["revision"] + 1, data["task_id"]))
        self.assertEqual(child["text_slots"][-1], slot)
        self.assertEqual(child["limits"], {"wall_seconds": 20.0, "max_steps": 5, "max_model_calls": 6,
                                           "max_action_retries": 0})
        self.assertEqual((chain["root_attempt_id"], chain["parent_attempt_id"], chain["sequence"]),
                         (parent_id, parent_id, 2))
        done = self.service.status(started["attempt_id"], wait_seconds=30)
        self.assertEqual(done["state"], "terminal", done)
        self.assertEqual(done["result"]["chain"]["sequence"], 2)
        self.assertEqual(done["result"]["chain"]["parent_attempt_id"], parent_id)
        # The child's usage accumulates on top of the parent's.
        self.assertGreaterEqual(done["result"]["chain"]["used"]["wall_seconds"], 10.0)

    def test_unchanged_task_keeps_revision(self):
        parent_id, data = self.parent()
        started = self.service.continue_task(parent_id, {}, "stub", "script", [{"action": "finish"}])
        child, _ = self.child_task(started)
        self.assertEqual(child["revision"], data["revision"])

    def test_refusals(self):
        parent_id, data = self.parent()
        cases = [
            ({"permissions": ["navigate", "delete_account"]}, "invalid_input"),
            ({"add_text_slots": [{"id": "name", "version": 1, "value": "other"}]}, "invalid_input"),
            ({"limits": {"wall_seconds": 999}}, "invalid_input"),
            ({"goal": ""}, "invalid_input"),
        ]
        for changes, code in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ApiError) as error:
                    self.service.continue_task(parent_id, changes, "stub", "script", [{"action": "finish"}])
                self.assertEqual(error.exception.code, code)
        unknown, _ = self.parent(attempt_id="c" * 32,
                                 actions=[{"id": 4, "operation": "submit_form", "target_id": "send-support",
                                           "effect": "unknown"}])
        with self.assertRaises(ApiError) as error:
            self.service.continue_task(unknown, {}, "stub", "script", [{"action": "finish"}])
        self.assertEqual(error.exception.code, "unresolved_effect")
        exhausted, _ = self.parent(attempt_id="d" * 32, used={"steps": 8, "model_calls": 1, "wall_seconds": 5})
        with self.assertRaises(ApiError) as error:
            self.service.continue_task(exhausted, {}, "stub", "script", [{"action": "finish"}])
        self.assertEqual(error.exception.code, "budget_exhausted")
        completed, _ = self.parent(attempt_id="e" * 32, status="completed", reason="verified", gaps=())
        with self.assertRaises(ApiError) as error:
            self.service.continue_task(completed, {}, "stub", "script", [{"action": "finish"}])
        self.assertEqual(error.exception.code, "continuation_refused")
        self.assertEqual(sorted(p.name for p in (self.service.root / "attempts").iterdir()),
                         sorted(c * 32 for c in "bcde"))

    def test_narrowed_permissions_are_allowed(self):
        parent_id, _ = self.parent()
        started = self.service.continue_task(parent_id, {"permissions": ["navigate"]}, "stub", "script",
                                             [{"action": "finish"}])
        child, _ = self.child_task(started)
        self.assertEqual(child["permissions"], ["navigate"])


class SupportPredicate(unittest.TestCase):
    """The content-gap fixture page and its predicate."""

    def setUp(self):
        self.port, self.run_id = free_port(), "support-run"
        self.process = subprocess.Popen([sys.executable, str(ROOT / "experiments/v05/site/server.py"),
                                         "--port", str(self.port), "--run-id", self.run_id],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.origin = f"http://127.0.0.1:{self.port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(self.origin + "/__state", timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)

    def tearDown(self):
        self.process.terminate()
        self.process.wait(timeout=5)

    def task(self, message=None):
        data = sample()
        data["fixture"] = {"origin": self.origin, "run_id": self.run_id}
        data["start_path"] = "/support"
        data["text_slots"] = [{"id": "name", "version": 1, "value": "Test User"},
                              {"id": "email", "version": 1, "value": "test@example.com"}]
        if message is not None:
            data["text_slots"].append({"id": "message", "version": 1, "value": message})
        data["criteria"] = [{"id": "sent", "kind": "postcondition", "predicate": "support_request_sent_once",
                             "args": {"name_slot": "name@1", "email_slot": "email@1"}}]
        return ExecutionTask.from_dict(data)

    def post(self, **fields):
        body = urllib.parse.urlencode(fields).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(self.origin + "/support", data=body), timeout=2).close()
        except urllib.error.HTTPError as exc:
            return exc.code
        return 200

    def verify(self, task):
        verifier = FixtureVerifier(task, self.baseline)
        return verifier(task, 2)[0]["status"]

    def test_message_must_come_from_a_declared_slot(self):
        task = self.task("Please call me back.")
        self.baseline = read_fixture(task, 2)
        self.assertEqual(self.post(name="Test User", email="test@example.com", message="Invented text"), 200)
        self.assertEqual(self.verify(task), "fail")

    def test_empty_message_is_rejected_by_the_server(self):
        task = self.task()
        self.baseline = read_fixture(task, 2)
        self.assertEqual(self.post(name="Test User", email="test@example.com", message=""), 400)
        self.assertEqual(self.verify(task), "fail")
        self.assertEqual(read_fixture(task, 2)["state"]["support_requests"], 0)

    def test_declared_message_passes_once(self):
        task = self.task("Please call me back.")
        self.baseline = read_fixture(task, 2)
        self.post(name="Test User", email="test@example.com", message="Please call me back.")
        self.assertEqual(self.verify(task), "pass")
        self.post(name="Test User", email="test@example.com", message="Please call me back.")
        self.assertEqual(self.verify(task), "fail")


if __name__ == "__main__":
    unittest.main()
