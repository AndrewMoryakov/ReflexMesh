"""Profile lifecycle regression source, without browser/SDK/model execution.

Unit cases exercise real temporary directories and keeper processes. Supervised
cases use the actual OwnedWorker cleanup proof and a narrow fake pinned backend;
they do not establish OS/CDP-controller exclusivity or SDK acceptance.
"""

import copy
import json
import multiprocessing as mp
import os
import signal
import sys
import tempfile
import time
import unittest
import urllib.request
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser
from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime import profile_owner as profiles_module
from reflexmesh.runtime.ownership import FixtureOwnership
from reflexmesh.runtime.profile_owner import AttemptProfiles, ProfileUnavailable
from reflexmesh.runtime.runner import AttemptGate, AttemptSupervisor, ControlledEnvironment, WorkerResult
from test_v05_browser_ownership import fake_pinned_backend
from test_v05_fixture import FixtureServerCase
from test_v05_optional_limits import finite_watchdog
from test_v05_runtime import sample


def hang(*args, **kwargs):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.01)


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux owned groups and mount IDs")
class ProfileCleanup(unittest.TestCase):
    def setUp(self):
        self.context = mp.get_context("fork")
        self.temp = tempfile.TemporaryDirectory(prefix="reflexmesh-profile-tests-")
        self.base = Path(self.temp.name)
        self.temp_patch = patch.object(profiles_module.tempfile, "gettempdir", return_value=self.temp.name)
        self.temp_patch.start()
        raw = sample()
        raw["limits"] = None
        self.task = ExecutionTask.from_dict(raw)
        self.owners = []

    def tearDown(self):
        try:
            for owner in self.owners:
                # No browser process exists in these direct filesystem units.
                owner.cleanup("closed", allowance=1)
                if owner._keeper is not None and not owner._keeper._released:
                    self.assertIn(owner._keeper.cleanup(1), ("closed", "forced"))
        finally:
            self.temp_patch.stop()
            self.temp.cleanup()

    def owner(self):
        owner = AttemptProfiles(self.context)
        fixture = FixtureOwnership(self.context, self.task, uuid.uuid4().hex)
        gate = AttemptGate(self.context, self.task, time.monotonic(),
                           fixture_owner=fixture, profile_client=owner.client)
        owner.client.bind_gate(gate)
        self.owners.append(owner)
        return owner

    def allocate(self, owner=None):
        owner = self.owner() if owner is None else owner
        owner.client.requested.value = 1  # Fixed request only; no path supplied.
        deadline = time.monotonic() + 2
        while not owner.client.ready.value and time.monotonic() < deadline:
            owner.service()
            time.sleep(0.005)
        self.assertEqual(owner.client.ready.value, 1)
        return owner, owner.client.claim(timeout=0.2)

    def test_enrollment_and_service_without_request_allocate_nothing(self):
        owner = self.owner()
        owner.service()
        self.assertIsNone(owner._keeper)
        self.assertEqual(list(self.base.iterdir()), [])
        self.assertEqual(owner.cleanup("closed")["status"], "not_created")

    def test_closed_and_forced_remove_nonempty_owned_profile_only(self):
        for process_cleanup in ("closed", "forced"):
            with self.subTest(process_cleanup=process_cleanup):
                owner, lease = self.allocate()
                foreign = self.base / ("foreign-" + process_cleanup)
                foreign.mkdir()
                (foreign / "keep").write_text("not browser data")
                nested = lease.path / "Default" / "Network"
                nested.mkdir(parents=True)
                (nested / "Cookies").write_text("private session cookie")
                (lease.path / "external-link").symlink_to(foreign, target_is_directory=True)
                result = owner.cleanup(process_cleanup, allowance=1)
                self.assertEqual(result["status"], "removed")
                self.assertFalse(lease.path.parent.exists())
                self.assertEqual((foreign / "keep").read_text(), "not browser data")
                self.assertNotIn(str(lease.path), json.dumps(result))
                self.assertNotIn("private session cookie", json.dumps(result))

    def test_unknown_worker_cleanup_retains_data_without_delete_command(self):
        owner, lease = self.allocate()
        marker = lease.path / "Cookies"
        marker.write_text("retain me")
        result = owner.cleanup("unknown", allowance=1)
        self.assertEqual(result, {"status": "retained", "reason": "worker_cleanup_unknown"})
        self.assertEqual(owner._control.command.value, 2)
        self.assertEqual(marker.read_text(), "retain me")

    def test_root_replacement_and_symlink_are_not_removed(self):
        for replacement in ("directory", "symlink"):
            with self.subTest(replacement=replacement):
                owner, lease = self.allocate()
                (lease.path / "original").write_text("owned")
                saved = lease.path.with_name("saved-original")
                lease.path.rename(saved)
                foreign = self.base / ("foreign-" + replacement)
                foreign.mkdir()
                (foreign / "keep").write_text("foreign")
                if replacement == "directory":
                    lease.path.mkdir(mode=0o700)
                    (lease.path / "replacement").write_text("keep replacement")
                else:
                    lease.path.symlink_to(foreign, target_is_directory=True)
                result = owner.cleanup("closed", allowance=1)
                self.assertEqual(result["status"], "retained")
                self.assertEqual((saved / "original").read_text(), "owned")
                self.assertEqual((foreign / "keep").read_text(), "foreign")
                self.assertTrue(lease.path.is_symlink() if replacement == "symlink" else
                                (lease.path / "replacement").is_file())

    def test_replaced_anchor_is_not_followed(self):
        owner, lease = self.allocate()
        (lease.path / "original").write_text("owned")
        saved = lease.path.parent.with_name("saved-anchor")
        lease.path.parent.rename(saved)
        foreign = self.base / "foreign-anchor"
        foreign.mkdir()
        (foreign / "keep").write_text("foreign")
        lease.path.parent.symlink_to(foreign, target_is_directory=True)
        self.assertEqual(owner.cleanup("closed", allowance=1)["status"], "retained")
        self.assertTrue(lease.path.parent.is_symlink())
        self.assertEqual((saved / lease.path.name / "original").read_text(), "owned")
        self.assertEqual((foreign / "keep").read_text(), "foreign")

    def test_missing_receipt_does_not_lose_keeper_cleanup_ownership(self):
        owner = self.owner()
        owner.client.requested.value = 1
        release = self.context.Value("i", 0, lock=False)
        allocate = AttemptProfiles._allocate

        def paused_allocate(keeper):
            while not release.value:
                time.sleep(0.005)
            allocate(keeper)

        deadline = time.monotonic() + 2
        with patch.object(AttemptProfiles, "_allocate", paused_allocate):
            owner.service()
        self.assertEqual(owner.client.ready.value, 0)
        release.value = 1
        while owner._control.prepared.value == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(owner._control.prepared.value, 1)
        # Parent never publishes ready; the worker's bounded wait fails.
        self.assertEqual(owner.client.ready.value, 0)
        with self.assertRaises(ProfileUnavailable):
            owner.client.claim(timeout=0.02)
        self.assertEqual(owner.cleanup("closed", allowance=1)["status"], "removed")
        self.assertEqual(list(self.base.iterdir()), [])

    def test_malformed_receipt_never_starts_a_browser(self):
        owner = self.owner()
        owner.client.ready.value = 1
        with self.assertRaises(ProfileUnavailable):
            owner.client.claim(timeout=0.02)
        self.assertIsNone(owner._keeper)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_foreign_registration_path_is_not_an_api(self):
        owner = self.owner()
        foreign = self.base / "foreign"
        foreign.mkdir()
        with self.assertRaises(TypeError):
            owner.client.claim(path=foreign)
        self.assertEqual(owner.cleanup("closed")["status"], "not_created")
        self.assertTrue(foreign.is_dir())

    def test_one_use_reservation_is_shared_across_client_copies(self):
        owner = self.owner()
        duplicate = copy.copy(owner.client)
        self.allocate(owner)
        self.assertFalse(duplicate._used)
        with self.assertRaises(ProfileUnavailable):
            duplicate.claim(timeout=0.02)
        self.assertEqual(owner.client._claimed.value, 1)

    def test_cancel_before_request_and_after_receipt_blocks_claim(self):
        for ready in (False, True):
            with self.subTest(receipt_ready=ready):
                owner = self.owner()
                if ready:
                    owner, _ = self.allocate(owner)
                    owner.client._used = False  # Isolate cancellation-at-consumption branch.
                owner.client._gate.stop("cancel_requested")
                with self.assertRaises(Exception) as caught:
                    owner.client.claim(timeout=0.02)
                self.assertEqual(str(caught.exception), "cancel_requested")
                if not ready:
                    owner.service()
                    self.assertIsNone(owner._keeper)

    def test_partial_allocation_failure_removes_known_empty_anchor(self):
        real_mkdir = os.mkdir

        def fail_inner(name, *args, **kwargs):
            if str(name).startswith("browser-use-user-data-dir-reflexmesh-"):
                raise OSError("private allocation failure")
            return real_mkdir(name, *args, **kwargs)

        owner = self.owner()
        owner.client.requested.value = 1
        with patch.object(profiles_module.os, "mkdir", side_effect=fail_inner):
            deadline = time.monotonic() + 2
            while not owner.client.ready.value and time.monotonic() < deadline:
                owner.service()
                time.sleep(0.005)
        self.assertEqual(owner.client.ready.value, -1)
        self.assertEqual(owner.cleanup("closed", allowance=1)["status"], "removed")
        self.assertEqual(list(self.base.iterdir()), [])

    def test_hung_allocation_does_not_block_supervisor_service_or_cleanup(self):
        owner = self.owner()
        owner.client.requested.value = 1
        with patch.object(AttemptProfiles, "_allocate", side_effect=hang):
            started = time.monotonic()
            owner.service()
            self.assertLess(time.monotonic() - started, 0.5)
        owner.close_requests()
        started = time.monotonic()
        result = owner.cleanup("closed", allowance=0.6)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(result["status"], "not_created")
        self.assertTrue(owner._keeper._released)

    def test_hung_cleanup_is_bounded_and_cannot_claim_removed(self):
        with patch.object(profiles_module, "_cleanup_directories", side_effect=hang):
            owner, lease = self.allocate()
        (lease.path / "Cookies").write_text("still private")
        started = time.monotonic()
        result = owner.cleanup("closed", allowance=0.6)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual((lease.path / "Cookies").read_text(), "still private")

    def test_keeper_death_cannot_be_repaired_from_a_worker_path(self):
        owner, lease = self.allocate()
        (lease.path / "Cookies").write_text("retain")
        os.kill(owner._keeper.pid, signal.SIGKILL)
        result = owner.cleanup("closed", allowance=1)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual((lease.path / "Cookies").read_text(), "retain")

    def test_fresh_root_mount_identity_is_checked_even_when_inode_matches(self):
        root = self.base / "root"
        root.mkdir(mode=0o700)
        parent = os.open(self.base, profiles_module.DIRECTORY_FLAGS)
        held = os.open("root", profiles_module.DIRECTORY_FLAGS, dir_fd=parent)
        try:
            directory = profiles_module._directory("root", held)
            with patch.object(profiles_module, "_mount_id", side_effect=lambda fd:
                              directory.mount_id if fd == held else directory.mount_id + 1):
                self.assertFalse(profiles_module._matches(parent, directory))
            self.assertTrue(root.is_dir())
        finally:
            os.close(held)
            os.close(parent)


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux owned groups and mount IDs")
class SupervisedProfileCleanup(FixtureServerCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="reflexmesh-supervised-profile-tests-")
        self.temp_patch = patch.object(profiles_module.tempfile, "gettempdir", return_value=self.temp.name)
        self.temp_patch.start()
        self.safe_temp_cleanup = True

    def tearDown(self):
        try:
            self.temp_patch.stop()
            if self.safe_temp_cleanup:
                self.temp.cleanup()
            else:
                # A broken process cleanup must not delete possibly-live data,
                # even in a failing regression fixture.
                self.temp._finalizer.detach()
        finally:
            super().tearDown()

    def stop_supervisor(self, supervisor):
        process = supervisor.process
        if process.pid is not None and not process._released:
            self.safe_temp_cleanup &= process.cleanup(1) in ("closed", "forced")
        supervisor.profiles.cleanup("unknown", allowance=1)
        keeper = supervisor.profiles._keeper
        if keeper is not None and keeper.pid is not None and not keeper._released:
            self.safe_temp_cleanup &= keeper.cleanup(1) in ("closed", "forced")
        supervisor.events.close()
        supervisor.slot_evidence.close()

    def run_browser(self, mode):
        task = self.task()
        task = replace(task, limits=replace(task.limits, wall_seconds=1.0))

        def strategy(gate, events):
            owner = gate.fixture_owner
            events.put(("fixture_claim", owner.acquire(1)))
            browser = FixtureBrowser(task)
            controlled = ControlledEnvironment(browser, gate, events, browser.admit, bootstrap=True)

            def configure(backend):
                if mode == "constructor_failure":
                    (Path(backend.user_data_dir) / "partial-data").write_text("private partial data")
                    raise OSError("partial backend startup")
                if mode == "hung_close":
                    backend.close = hang

            try:
                with fake_pinned_backend(task, owner, configure=configure):
                    controlled.reset(task.goal)
                    (browser._profile_path / "Cookies").write_text("private browser data")
            finally:
                controlled.close()
            return WorkerResult("executor_error", "test_stop")

        supervisor = AttemptSupervisor(task, strategy)
        try:
            with finite_watchdog(8), patch("reflexmesh.runtime.runner.GRACE_SECONDS", 0.05), \
                    patch("reflexmesh.runtime.runner.KILL_SECONDS", 0.4):
                result = supervisor.run()
        finally:
            self.stop_supervisor(supervisor)
        return supervisor, result

    def test_normal_backend_close_removes_profile_after_real_group_absence(self):
        supervisor, result = self.run_browser("normal")
        self.assertEqual(result["worker_cleanup"], "closed")
        self.assertEqual(result["profile_cleanup"]["status"], "removed")
        self.assertFalse(supervisor.profiles._lease.path.parent.exists())
        self.assertNotIn(str(supervisor.profiles._lease.path), json.dumps(result))
        self.assertNotIn(supervisor.fixture_owner.browser_secret, json.dumps(result))
        with urllib.request.urlopen(self.origin + "/__state", timeout=1) as response:
            self.assertEqual(json.load(response)["phase"], "released")

    def test_hung_backend_close_is_forced_before_profile_removal(self):
        supervisor, result = self.run_browser("hung_close")
        self.assertEqual(result["worker_cleanup"], "forced")
        self.assertEqual(result["profile_cleanup"]["status"], "removed")
        self.assertFalse(supervisor.profiles._lease.path.parent.exists())
        self.assertEqual(result["stop_reason"], "deadline")

    def test_partial_backend_constructor_failure_still_cleans_owned_profile(self):
        supervisor, result = self.run_browser("constructor_failure")
        self.assertEqual(result["worker_cleanup"], "closed")
        self.assertEqual(result["profile_cleanup"]["status"], "removed")
        self.assertFalse(supervisor.profiles._lease.path.parent.exists())

    def test_worker_killed_before_consuming_receipt_leaves_no_unowned_profile(self):
        task = self.task()
        task = replace(task, limits=replace(task.limits, wall_seconds=2.0))

        def strategy(gate, events):
            gate.profile_client.requested.value = 1
            while gate.profile_client.ready.value == 0:
                time.sleep(0.005)
            os.kill(os.getpid(), signal.SIGKILL)

        supervisor = AttemptSupervisor(task, strategy)
        try:
            with finite_watchdog(8), patch("reflexmesh.runtime.runner.GRACE_SECONDS", 0.05):
                result = supervisor.run()
        finally:
            self.stop_supervisor(supervisor)
        self.assertEqual(result["worker_cleanup"], "closed")
        self.assertEqual(result["profile_cleanup"]["status"], "removed")
        self.assertFalse(supervisor.profiles._lease.path.parent.exists())


if __name__ == "__main__":
    unittest.main()
