"""One-shot persisted continuation admission (INV-10/11/12); no real child processes."""

import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reflexmesh.api.service import ApiError, TaskService
from test_v05_runtime import sample


@unittest.skipUnless(os.name == "posix", "the execution runtime requires POSIX")
class HandoffConsumption(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = TaskService(Path(self.tmp.name) / "state", max_concurrent=4)
        self.parent_id = "a" * 32
        self.parent_dir = self.service.root / "attempts" / self.parent_id
        (self.parent_dir / "out").mkdir(parents=True)
        self.task = sample()
        self.task["limits"] = {"wall_seconds": 30, "max_steps": 8, "max_model_calls": 8,
                               "max_action_retries": 0}
        (self.parent_dir / "task.json").write_text(json.dumps(self.task))
        (self.parent_dir / "request.json").write_text(json.dumps({"attempt_id": self.parent_id}))
        result = {"attempt_status": "incomplete", "handoff": {"continuation": {"allowed": True}},
                  "chain": {"root_attempt_id": self.parent_id, "sequence": 1,
                            "used": {"steps": 3, "model_calls": 2, "wall_seconds": 10}}}
        (self.parent_dir / "out" / "result.json").write_text(json.dumps(result))
        self.process = Mock()
        self.process.poll.return_value = None
        self.popen = self.enterContext(patch("reflexmesh.api.service.subprocess.Popen", return_value=self.process))

    def resume(self, service=None, changes=None):
        return (service or self.service).continue_task(
            self.parent_id, {} if changes is None else changes, "stub", "script", [{"action": "finish"}])

    def assert_consumed(self, service=None):
        with self.assertRaises(ApiError) as error:
            self.resume(service)
        self.assertEqual(error.exception.code, "continuation_refused")

    def test_replay_after_child_finishes_and_service_restarts(self):
        child = self.resume()
        self.process.poll.return_value = 0
        self.assert_consumed()
        restarted = TaskService(self.service.root, max_concurrent=4)
        self.assert_consumed(restarted)
        self.assertEqual(self.popen.call_count, 1)
        claim = json.loads((self.parent_dir / "continuation.json").read_text())
        self.assertEqual(claim, {"parent_attempt_id": self.parent_id, "child_attempt_id": child["attempt_id"]})
        self.assertEqual(child["chain_sequence"], 2)
        self.assertEqual(child["limits"], {"wall_seconds": 20.0, "max_steps": 5,
                                            "max_model_calls": 6, "max_action_retries": 0})

    def test_concurrent_callers_start_only_one_child(self):
        # Separate service locks exercise the filesystem arbitration, not just a mutex.
        second = TaskService(self.service.root, max_concurrent=4)
        barrier = threading.Barrier(2)
        def resume_together(service):
            barrier.wait(timeout=5)
            try:
                return self.resume(service)
            except ApiError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(resume_together, (self.service, second)))
        self.assertEqual(sum(isinstance(row, dict) for row in outcomes), 1)
        self.assertEqual(outcomes.count("continuation_refused"), 1)
        self.assertEqual(self.popen.call_count, 1)

    def test_invalid_busy_and_shutdown_requests_do_not_consume(self):
        with self.assertRaises(ApiError) as error:
            self.resume(changes={"goal": ""})
        self.assertEqual(error.exception.code, "invalid_input")
        with patch.object(self.service, "_running", return_value=4):
            with self.assertRaises(ApiError) as error:
                self.resume()
        self.assertEqual(error.exception.code, "busy")
        self.service.closed = True
        with self.assertRaises(ApiError) as error:
            self.resume()
        self.assertEqual(error.exception.code, "unavailable")
        self.assertFalse((self.parent_dir / "continuation.json").exists())
        self.popen.assert_not_called()
        self.service.closed = False
        self.resume()
        self.popen.assert_called_once()

    def test_launch_failure_leaves_handoff_consumed(self):
        self.popen.side_effect = OSError("launch failed")
        with self.assertRaises(OSError):
            self.resume()
        self.assert_consumed(TaskService(self.service.root, max_concurrent=4))
        self.assertEqual(self.popen.call_count, 1)

    def test_persistence_failure_starts_nothing_and_fails_closed(self):
        with patch("reflexmesh.api.service.os.fsync", side_effect=OSError("sync failed")):
            with self.assertRaises(OSError):
                self.resume()
        self.popen.assert_not_called()
        self.assert_consumed(TaskService(self.service.root, max_concurrent=4))

    def test_incomplete_claim_is_never_reused(self):
        (self.parent_dir / "continuation.json").touch()
        self.assert_consumed()
        self.popen.assert_not_called()

    def test_directory_sync_failure_starts_nothing_and_fails_closed(self):
        with patch("reflexmesh.api.service.os.fsync", side_effect=[None, OSError("directory sync failed")]):
            with self.assertRaises(OSError):
                self.resume()
        self.popen.assert_not_called()
        self.assert_consumed(TaskService(self.service.root, max_concurrent=4))

    def test_claim_file_and_directory_are_synced_before_launch(self):
        events = []
        real_sync = os.fsync
        def sync(fd):
            real_sync(fd)
            events.append("sync")
        def launch(*args, **kwargs):
            self.assertEqual(events, ["sync", "sync"])
            self.assertTrue((self.parent_dir / "continuation.json").is_file())
            events.append("launch")
            return self.process
        self.popen.side_effect = launch
        with patch("reflexmesh.api.service.os.fsync", side_effect=sync):
            self.resume()
        self.assertEqual(events, ["sync", "sync", "launch"])


if __name__ == "__main__":
    unittest.main()
