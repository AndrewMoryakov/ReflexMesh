"""Verify server state and CLI behavior using the isolated real HTTP fixture."""

import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.verification.verifier import FixtureVerifier, read_fixture
from test_v05_runtime import sample


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FixtureChecks(unittest.TestCase):
    def setUp(self):
        self.port = available_port()
        self.run_id = "fixture-test"
        self.process = subprocess.Popen([sys.executable, str(ROOT / "experiments/v05/site/server.py"),
                                         "--port", str(self.port), "--run-id", self.run_id,
                                         "--slow-seconds", "0.2"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.origin = f"http://127.0.0.1:{self.port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(self.origin + "/__state", timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.02)
        else:
            self.fail("fixture server did not start")

    def tearDown(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)

    def task(self, predicate="account_intact", args=None):
        data = copy.deepcopy(sample())
        data["fixture"] = {"origin": self.origin, "run_id": self.run_id}
        data["criteria"] = [{"id": "criterion", "kind": "postcondition", "predicate": predicate,
                             "args": args if args is not None else {}}]
        return ExecutionTask.from_dict(data)

    def test_submission_count_and_exact_slot_values(self):
        data = copy.deepcopy(sample())
        data["fixture"] = {"origin": self.origin, "run_id": self.run_id}
        data["criteria"] = [{"id": "sent", "kind": "postcondition", "predicate": "form_submitted_once",
                             "args": {"name_slot": "name@1", "email_slot": "email@1"}}]
        data["text_slots"].append({"id": "email", "version": 1, "value": "test@example.com"})
        task = ExecutionTask.from_dict(data)
        baseline = read_fixture(task, 1)
        verifier = FixtureVerifier(task, baseline)
        self.assertEqual(verifier(task, 1)[0]["status"], "fail")
        values = urllib.parse.urlencode({"name": "secret-text", "email": "test@example.com"}).encode()
        urllib.request.urlopen(urllib.request.Request(self.origin + "/form", data=values), timeout=1).close()
        self.assertEqual(verifier(task, 1)[0]["status"], "pass")
        urllib.request.urlopen(urllib.request.Request(self.origin + "/form", data=values), timeout=1).close()
        self.assertEqual(verifier(task, 1)[0]["status"], "fail")

    def test_historic_navigation_does_not_prove_current_page(self):
        task = self.task("current_page", {"path": "/reports", "target_id": "reports"})
        verifier = FixtureVerifier(task, read_fixture(task, 1))
        urllib.request.urlopen(self.origin + "/reports", timeout=1).close()
        urllib.request.urlopen(self.origin + "/form", timeout=1).close()
        observation = {"run_id": self.run_id, "url": self.origin + "/form", "target_ids": ["form"]}
        self.assertEqual(verifier(task, 1, observation)[0]["status"], "fail")
        self.assertEqual(verifier(task, 1, None)[0]["status"], "unknown")

    def test_delayed_old_export_cannot_satisfy_new_fixture(self):
        old_task = self.task("export_completed_once")
        old_verifier = FixtureVerifier(old_task, read_fixture(old_task, 1))
        sent = threading.Event()

        def delayed_post():
            sent.set()
            urllib.request.urlopen(urllib.request.Request(self.origin + "/slow/export", data=b""),
                                   timeout=2).close()

        thread = threading.Thread(target=delayed_post)
        thread.start()
        self.assertTrue(sent.wait(1))
        new_port = available_port()
        new_run = "isolated-next-run"
        new_process = subprocess.Popen([sys.executable, str(ROOT / "experiments/v05/site/server.py"),
                                        "--port", str(new_port), "--run-id", new_run,
                                        "--slow-seconds", "0.2"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            data = copy.deepcopy(sample())
            data["fixture"] = {"origin": f"http://127.0.0.1:{new_port}", "run_id": new_run}
            data["criteria"] = [{"id": "export", "kind": "postcondition",
                                 "predicate": "export_completed_once", "args": {}}]
            new_task = ExecutionTask.from_dict(data)
            for _ in range(50):
                try:
                    new_baseline = read_fixture(new_task, 0.1)
                    break
                except OSError:
                    time.sleep(0.02)
            else:
                self.fail("new fixture did not start")
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(old_verifier(old_task, 1)[0]["status"], "pass")
            self.assertEqual(FixtureVerifier(new_task, new_baseline)(new_task, 1)[0]["status"], "fail")
        finally:
            new_process.terminate()
            new_process.wait(timeout=2)

    def test_wrong_run_is_rejected(self):
        wrong = copy.deepcopy(sample())
        wrong["fixture"] = {"origin": self.origin, "run_id": "other-run"}
        with self.assertRaises(ValueError):
            read_fixture(ExecutionTask.from_dict(wrong), 1)

    def test_cli_explains_missing_browser(self):
        if __import__("importlib").util.find_spec("browser_use") is not None:
            self.skipTest("Browser Use installed; exercise the full integration instead")
        data = copy.deepcopy(sample())
        data["fixture"] = {"origin": self.origin, "run_id": self.run_id}
        with tempfile.TemporaryDirectory() as tmp:
            task_path, script_path, output_dir = Path(tmp) / "task.json", Path(tmp) / "script.json", Path(tmp) / "out"
            task_path.write_text(json.dumps(data), encoding="utf-8")
            script_path.write_text('[{"action":"finish"}]', encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
            proc = subprocess.run([sys.executable, "-m", "reflexmesh", "run", "--input", str(task_path),
                                   "--output-dir", str(output_dir), "--routing-provider", "stub",
                                   "--action-provider", "script", "--script", str(script_path)],
                                  env=env, capture_output=True, timeout=5)
            self.assertEqual(proc.returncode, 3, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["stop_reason"], "executor_unavailable")
            self.assertEqual(json.loads((output_dir / "result.json").read_text()), result)
            self.assertNotIn("secret-text", (output_dir / "trace.jsonl").read_text())
            self.assertNotIn("secret-text", (output_dir / "evidence.jsonl").read_text())
            self.assertNotIn("secret-text", (output_dir / "result.json").read_text())
            self.assertTrue((output_dir / "evidence.jsonl").is_file())
            for ref in result["evidence_refs"]:
                self.assertIn(ref, (output_dir / "evidence.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
