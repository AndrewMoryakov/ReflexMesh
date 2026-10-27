"""Fixture Task API artifact privacy (INV-14); all child launches are mocked."""

import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reflexmesh.api import service as api


@contextmanager
def ambient_umask(mask):
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


@unittest.skipUnless(os.name == "posix", "the fixture runtime requires POSIX")
class ArtifactPrivacy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        patcher = mock.patch.object(api.subprocess, "Popen", autospec=True)
        self.popen = patcher.start()
        self.addCleanup(patcher.stop)
        self.popen.return_value.poll.return_value = 0

    def assert_mode(self, path, expected):
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, str(path))

    def start(self, service, **extra):
        # Exercise persistence directly, independent of validation/browser fixtures.
        task = {"text_slots": [{"id": "private", "version": 1,
                                "value": "synthetic-private-value", "sensitivity": "secret"}]}
        return service._start(task, "stub", "script", [{"action": "finish"}],
                              extra.get("chain"), extra.get("faults"))

    def test_new_state_and_intermediate_directories_are_private(self):
        self.base.chmod(0o755)
        for mask in (0o000, 0o022, 0o777):
            with self.subTest(umask=oct(mask)), ambient_umask(mask):
                parent = self.base / str(mask)
                root = parent / "nested" / "state"
                api.TaskService(root, env={})
                for path in (parent, parent / "nested", root, root / "attempts"):
                    self.assert_mode(path, 0o700)
                self.assert_mode(self.base, 0o755)
        self.popen.assert_not_called()

    def test_reused_root_and_attempts_are_tightened_without_changing_ancestors(self):
        root = self.base / "state"
        attempts = root / "attempts"
        attempts.mkdir(parents=True)
        self.base.chmod(0o755)
        root.chmod(0o777)
        attempts.chmod(0o775)
        api.TaskService(root, env={})
        self.assert_mode(root, 0o700)
        self.assert_mode(attempts, 0o700)
        self.assert_mode(self.base, 0o755)

    def test_attempts_symlink_is_rejected_without_chmod_of_its_target(self):
        root = self.base / "state"
        outside = self.base / "outside"
        root.mkdir()
        outside.mkdir()
        outside.chmod(0o755)
        (root / "attempts").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            api.TaskService(root, env={})
        self.assert_mode(outside, 0o755)
        self.popen.assert_not_called()

    def test_failure_to_tighten_reused_root_prevents_initialization(self):
        root = self.base / "state"
        root.mkdir()
        root.chmod(0o755)
        with mock.patch.object(Path, "chmod", side_effect=PermissionError("synthetic chmod failure")):
            with self.assertRaises(PermissionError):
                api.TaskService(root, env={})
        self.assertFalse((root / "attempts").exists())
        self.popen.assert_not_called()

    def test_all_inputs_and_logs_have_explicit_owner_only_modes(self):
        chain = {"parent_attempt_id": "a" * 32}
        faults = {"synthetic_fault": True}
        for mask in (0o000, 0o022, 0o777):
            with self.subTest(umask=oct(mask)), ambient_umask(mask):
                service = api.TaskService(self.base / str(mask), env={})
                # Handoff consumption has its own tests; isolate artifact creation here.
                with mock.patch.object(service, "_consume_handoff") as consume:
                    started = self.start(service, chain=chain, faults=faults)
                directory = service.root / "attempts" / started["attempt_id"]
                consume.assert_called_once_with(chain["parent_attempt_id"], started["attempt_id"])
                self.assert_mode(directory, 0o700)
                expected = {"task.json", "script.json", "chain.json", "faults.json",
                            "request.json", "stdout.json", "stderr.log"}
                self.assertEqual({p.name for p in directory.iterdir()}, expected)
                for name in expected:
                    self.assert_mode(directory / name, 0o600)
                saved_task = json.loads((directory / "task.json").read_text(encoding="utf-8"))
                self.assertEqual(saved_task["text_slots"][0]["value"], "synthetic-private-value")
                self.assertEqual(json.loads((directory / "chain.json").read_text(encoding="utf-8")), chain)
                self.assertEqual(json.loads((directory / "faults.json").read_text(encoding="utf-8")), faults)
                kwargs = self.popen.call_args.kwargs
                self.assertEqual(kwargs["cwd"], directory)
                # Popen applies this in the child, covering result/trace/evidence files too.
                self.assertEqual(kwargs["umask"], 0o077)
                self.assertTrue(kwargs["stdout"].closed)
                self.assertTrue(kwargs["stderr"].closed)

    def test_service_does_not_change_the_parent_process_umask(self):
        with ambient_umask(0o000):
            with mock.patch.object(api.os, "umask", side_effect=AssertionError("global umask changed")):
                service = api.TaskService(self.base / "state", env={})
                self.start(service)
        self.popen.assert_called_once()
        self.assertEqual(self.popen.call_args.kwargs["umask"], 0o077)

    def test_log_handles_close_and_artifacts_stay_private_when_launch_fails(self):
        service = api.TaskService(self.base / "state", env={})
        self.popen.side_effect = OSError("synthetic launch failure")
        with ambient_umask(0o000), self.assertRaises(OSError):
            self.start(service)
        kwargs = self.popen.call_args.kwargs
        self.assertTrue(kwargs["stdout"].closed)
        self.assertTrue(kwargs["stderr"].closed)
        self.assertEqual(service.processes, {})
        self.assert_mode(kwargs["cwd"], 0o700)
        for path in kwargs["cwd"].iterdir():
            self.assert_mode(path, 0o600)

    def test_stdout_closes_if_stderr_cannot_be_created(self):
        service = api.TaskService(self.base / "state", env={})
        original = api._open_private
        opened = []

        def fail_stderr(path):
            if path.name == "stderr.log":
                raise PermissionError("synthetic log creation failure")
            stream = original(path)
            opened.append(stream)
            return stream

        with mock.patch.object(api, "_open_private", side_effect=fail_stderr):
            with self.assertRaises(PermissionError):
                self.start(service)
        self.assertTrue(opened)
        self.assertTrue(all(stream.closed for stream in opened))
        self.popen.assert_not_called()

    def test_file_permission_failure_closes_descriptor_and_prevents_launch(self):
        service = api.TaskService(self.base / "state", env={})
        with mock.patch.object(api.os, "fchmod", side_effect=PermissionError("synthetic chmod failure")):
            with mock.patch.object(api.os, "close", wraps=os.close) as close:
                with self.assertRaises(PermissionError):
                    self.start(service)
                close.assert_called_once()
        self.popen.assert_not_called()
        self.assertEqual(service.processes, {})

    def test_private_file_creation_never_overwrites_or_follows_existing_entries(self):
        target = self.base / "existing.json"
        target.write_text("unchanged", encoding="utf-8")
        target.chmod(0o644)
        link = self.base / "linked.json"
        link.symlink_to(target)
        for path in (target, link):
            with self.subTest(path=path.name), self.assertRaises(FileExistsError):
                api._write_private_json(path, {"value": "replacement"})
        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")
        self.assert_mode(target, 0o644)


if __name__ == "__main__":
    unittest.main()
