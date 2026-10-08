"""Profile/fixture teardown integration source using actual owned subprocesses.

The HTTP fixture and supervisor are real. Workers only simulate browser data
writes; these cases do not establish real-browser, SDK, CDP, or model acceptance.
No passing execution constraint is fabricated. Private paths travel through a
test-only file, never through the supervisor's public event/result channel.
"""

import json
import multiprocessing as mp
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.runtime import profile_owner as profiles_module
from reflexmesh.runtime.ownership import FixtureOwnership, OwnershipError
from reflexmesh.runtime.profile_owner import AttemptProfiles
from reflexmesh.runtime.runner import AttemptSupervisor, WorkerResult
from reflexmesh.verification.verifier import FixtureVerifier, read_fixture
from test_v05_fixture import FixtureServerCase
from test_v05_optional_limits import finite_watchdog


LINUX_OWNED_WORKER = (sys.platform.startswith("linux") and hasattr(os, "WNOWAIT") and
                      hasattr(signal, "setitimer") and "fork" in mp.get_all_start_methods())
PRIVATE_BROWSER_DATA = "private-cookie-and-local-storage-profile-runtime-sentinel"


def claim_fixture(gate, events):
    # This HTTP lifecycle claim cannot attest browser-controller ownership.
    events.put(("fixture_claim", gate.fixture_owner.acquire(1)))


def write_browser_data(path, private_receipt):
    nested = path / "Default" / "Network"
    nested.mkdir(parents=True)
    (nested / "Cookies").write_text(PRIVATE_BROWSER_DATA, encoding="utf-8")
    storage = path / "Default" / "Local Storage"
    storage.mkdir()
    (storage / "state").write_text(PRIVATE_BROWSER_DATA, encoding="utf-8")
    private_receipt.write_text(str(path), encoding="utf-8")


def never_return():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.01)


@unittest.skipUnless(LINUX_OWNED_WORKER, "requires Linux owned workers and a SIGALRM watchdog")
class ProfileCleanupRuntime(FixtureServerCase):
    def setUp(self):
        super().setUp()
        self.context = mp.get_context("fork")
        self.scratch = tempfile.TemporaryDirectory(prefix="reflexmesh-profile-runtime-")
        self.base = Path(self.scratch.name) / "profiles"
        self.base.mkdir()
        self.private_receipt = Path(self.scratch.name) / "private-profile-path"
        self.temp_patch = patch.object(profiles_module.tempfile, "gettempdir",
                                       return_value=str(self.base))
        self.temp_patch.start()
        self.supervisors = []

    def tearDown(self):
        stopped = True
        try:
            for supervisor in self.supervisors:
                worker = supervisor.process
                proof = "closed" if worker._released else "unknown"
                if worker.pid is not None and not worker._released:
                    proof = worker.cleanup(1)
                    stopped = stopped and proof in ("closed", "forced")
                supervisor.profiles.close_requests()
                supervisor.profiles.cleanup(proof, allowance=1)
                keeper = supervisor.profiles._keeper
                if keeper is not None and keeper.pid is not None and not keeper._released:
                    stopped = keeper.cleanup(1) in ("closed", "forced") and stopped
                supervisor.events.close()
                supervisor.slot_evidence.close()
        finally:
            self.temp_patch.stop()
            super().tearDown()
            if stopped:
                # Runtime retention is intentional in uncertainty cases. The
                # test owns this whole scratch root and removes it separately,
                # only after stopping its real worker and keeper processes.
                self.scratch.cleanup()
            else:
                # An armed TemporaryDirectory finalizer would later delete
                # retained data even though process absence was not proved.
                self.scratch._finalizer.detach()
        self.assertTrue(stopped, "test-owned subprocess could not be safely stopped")

    def task(self, *, wall=None):
        task = super().task()
        return replace(task, limits=replace(task.limits, wall_seconds=wall))

    def supervisor(self, task, strategy):
        supervisor = AttemptSupervisor(task, strategy, verifier_factory=FixtureVerifier)
        self.supervisors.append(supervisor)
        return supervisor

    def populated_worker(self, gate, events):
        claim_fixture(gate, events)
        lease = gate.profile_client.claim(timeout=2)
        write_browser_data(lease.path, self.private_receipt)
        return WorkerResult("finish")

    def run_bounded(self, supervisor):
        # This is an independent test watchdog, not an implicit wall limit on
        # tasks whose wall_seconds is None. Production cleanup keeps its own
        # normal two-second budget; no tight wall-clock assertion is needed.
        with finite_watchdog(12), patch("reflexmesh.runtime.runner.GRACE_SECONDS", 0.2):
            result = supervisor.run()
        self.assertTrue(supervisor.process._released, "owned worker was not reaped")
        keeper = supervisor.profiles._keeper
        if keeper is not None:
            self.assertTrue(keeper._released, "profile keeper was not reaped")
        self.assert_private(supervisor, result)
        return result

    def run_cancelled_after(self, supervisor, ready):
        stop = threading.Event()
        cancelled = []

        def cancel_when_ready():
            while not stop.wait(0.01):
                if ready.value:
                    cancelled.append(supervisor.cancel())
                    return

        cancellation = threading.Thread(target=cancel_when_ready, daemon=True)
        cancellation.start()
        try:
            result = self.run_bounded(supervisor)
        finally:
            stop.set()
            cancellation.join(1)
        self.assertFalse(cancellation.is_alive())
        self.assertEqual(cancelled, [True])
        return result

    def private_path(self):
        path = Path(self.private_receipt.read_text(encoding="utf-8"))
        self.assertEqual(path.parent.parent, self.base)
        return path

    def assert_private(self, supervisor, result):
        serialized = json.dumps(result, allow_nan=False)
        for private in (str(self.base), PRIVATE_BROWSER_DATA, "secret-text",
                        supervisor.fixture_owner.browser_secret,
                        supervisor.fixture_owner._owner_secret):
            self.assertNotIn(private, serialized)
        self.assertNotIn("fixture_claim", [row["kind"] for row in result["trace"]])

    def assert_quarantined(self, task):
        self.assertEqual(read_fixture(task, 1)["phase"], "quarantined")
        contender = FixtureOwnership(self.context, task, "next-profile-runtime-attempt")
        with self.assertRaises(OwnershipError) as blocked:
            contender.acquire(1)
        self.assertEqual(blocked.exception.reason, "fixture_busy")

    def assert_browser_data_retained(self):
        path = self.private_path()
        self.assertEqual((path / "Default" / "Network" / "Cookies").read_text(encoding="utf-8"),
                         PRIVATE_BROWSER_DATA)
        self.assertEqual((path / "Default" / "Local Storage" / "state").read_text(encoding="utf-8"),
                         PRIVATE_BROWSER_DATA)

    def test_fixture_release_observes_confirmed_worker_exit_and_removed_profile(self):
        task = self.task()
        supervisor = self.supervisor(task, self.populated_worker)
        real_release = supervisor.fixture_owner.release
        observed = []

        def release_after_removal(cleanup, timeout):
            path = self.private_path()
            observed.append((cleanup, supervisor.process._released, path.parent.exists(),
                             read_fixture(task, 1)["phase"]))
            return real_release(cleanup, timeout=timeout)

        with patch.object(supervisor.fixture_owner, "release", side_effect=release_after_removal):
            result = self.run_bounded(supervisor)
        self.assertEqual(observed, [("closed", True, False, "quarantined")])
        self.assertEqual(result["worker_cleanup"], "closed")
        self.assertEqual(result["profile_cleanup"]["status"], "removed")
        self.assertEqual(result["cleanup"], "closed")
        self.assertEqual(read_fixture(task, 1)["phase"], "released")
        self.assertNotEqual(result["attempt_status"], "completed")
        self.assertIsNone(result["budget"]["limits"]["wall_seconds"])

    def test_unknown_worker_evidence_retains_profile_and_prevents_fixture_reuse(self):
        task = self.task()
        supervisor = self.supervisor(task, self.populated_worker)
        real_cleanup = supervisor.process.cleanup
        actual_cleanup = []

        def lose_absence_evidence(allowance):
            # Reap the real owned process. Fault only the evidence presented to
            # the supervisor; no fake success is substituted for process proof.
            actual_cleanup.append(real_cleanup(allowance))
            return "unknown"

        with patch.object(supervisor.process, "cleanup", side_effect=lose_absence_evidence):
            result = self.run_bounded(supervisor)
        self.assertEqual(actual_cleanup, ["closed"])
        self.assertEqual(result["worker_cleanup"], "unknown")
        self.assertEqual(result["profile_cleanup"],
                         {"status": "retained", "reason": "worker_cleanup_unknown"})
        self.assertEqual(result["cleanup"], "unknown")
        self.assertEqual(supervisor.profiles._control.command.value, 2)
        self.assert_browser_data_retained()
        self.assert_quarantined(task)

    def test_dead_gate_lock_does_not_discard_actual_worker_cleanup_proof(self):
        task = self.task()
        lock_taken = self.context.Value("i", 0, lock=False)

        def die_holding_gate(gate, events):
            claim_fixture(gate, events)
            lease = gate.profile_client.claim(timeout=2)
            write_browser_data(lease.path, self.private_receipt)
            gate._acquire()
            lock_taken.value = 1
            # No other actor releases this lock. The group can be confirmed
            # absent even though the terminal budget snapshot stays unknown.
            os._exit(23)

        supervisor = self.supervisor(task, die_holding_gate)
        result = self.run_bounded(supervisor)
        self.assertEqual(lock_taken.value, 1, "dead-gate branch was not reached")
        self.assertEqual(result["worker_cleanup"], "closed")
        self.assertEqual(result["profile_cleanup"]["status"], "removed")
        self.assertEqual(supervisor.profiles._control.command.value, 1)
        self.assertFalse(self.private_path().parent.exists())
        self.assertEqual(result["cleanup"], "unknown")
        self.assert_quarantined(task)

    def allocation_stall(self, task, *, cancel):
        entered = self.context.Value("i", 0, lock=False)
        real_allocate = AttemptProfiles._allocate

        def allocate_then_stall(profiles):
            real_allocate(profiles)
            # Allocation is complete but the keeper never publishes prepared.
            # Only the test inspects its private receipt to clean up afterwards.
            lease = profiles.client._receipt()
            write_browser_data(lease.path, self.private_receipt)
            entered.value = 1
            never_return()

        def wait_for_profile(gate, events):
            claim_fixture(gate, events)
            # Longer than the finite task case below, so a local claim timeout
            # cannot masquerade as supervisor cancellation/deadline handling.
            gate.profile_client.claim(timeout=10)
            return WorkerResult("executor_error", "unexpected_profile_receipt")

        supervisor = self.supervisor(task, wait_for_profile)
        with patch.object(AttemptProfiles, "_allocate", new=allocate_then_stall):
            result = (self.run_cancelled_after(supervisor, entered) if cancel else
                      self.run_bounded(supervisor))
        self.assertEqual(entered.value, 1, "allocation-stall branch was not reached")
        self.assertIn(result["worker_cleanup"], ("closed", "forced"))
        self.assertEqual(result["profile_cleanup"]["status"], "unknown")
        self.assertEqual(result["cleanup"], "unknown")
        self.assert_browser_data_retained()
        self.assert_quarantined(task)
        return result

    def test_cancel_interrupts_unlimited_attempt_while_allocator_is_stalled(self):
        result = self.allocation_stall(self.task(), cancel=True)
        self.assertEqual((result["attempt_status"], result["stop_reason"]),
                         ("cancelled", "cancel_requested"))
        self.assertIsNone(result["budget"]["limits"]["wall_seconds"])
        self.assertIsNone(result["budget"]["remaining_wall_seconds"])

    def test_deadline_interrupts_attempt_while_allocator_is_stalled(self):
        result = self.allocation_stall(self.task(wall=2), cancel=False)
        self.assertEqual((result["attempt_status"], result["stop_reason"]),
                         ("incomplete", "deadline"))

    def failed_directory_adoption(self, *, profile):
        task = self.task()
        failed_open = self.context.Value("i", 0, lock=False)
        cleanup_called = self.context.Value("i", 0, lock=False)

        def bootstrap(gate, events):
            claim_fixture(gate, events)
            gate.profile_client.claim(timeout=2)
            return WorkerResult("executor_error", "unexpected_profile_receipt")

        supervisor = self.supervisor(task, bootstrap)
        anchor = self.base / supervisor.profiles._anchor_name
        created = anchor / supervisor.profiles._profile_name if profile else anchor
        fail_name = supervisor.profiles._profile_name if profile else supervisor.profiles._anchor_name
        real_open = os.open

        def fail_after_mkdir(path, flags, mode=0o777, *, dir_fd=None):
            if path == fail_name and dir_fd is not None:
                # This open follows the real successful mkdir. A private test
                # marker proves the unadopted directory was left untouched.
                (created / "unadopted-data").write_text(PRIVATE_BROWSER_DATA, encoding="utf-8")
                failed_open.value = 1
                raise OSError("directory handle unavailable after creation")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        def unexpected_cleanup(*args):
            cleanup_called.value = 1
            raise AssertionError("incomplete descriptor adoption cannot authorize traversal")

        with patch.object(profiles_module.os, "open", new=fail_after_mkdir), \
                patch.object(profiles_module, "_cleanup_directories", new=unexpected_cleanup):
            result = self.run_bounded(supervisor)
        self.assertEqual(failed_open.value, 1, "post-mkdir failure branch was not reached")
        self.assertEqual(cleanup_called.value, 0, "cleanup traversed an incompletely adopted root")
        self.assertEqual(result["worker_cleanup"], "closed")
        self.assertEqual(result["profile_cleanup"]["status"], "unknown")
        self.assertEqual(result["cleanup"], "unknown")
        self.assertIsNone(supervisor.profiles._lease)
        self.assertEqual((created / "unadopted-data").read_text(encoding="utf-8"),
                         PRIVATE_BROWSER_DATA)
        self.assert_quarantined(task)

    def test_failed_anchor_open_after_mkdir_retains_root_and_quarantines_fixture(self):
        self.failed_directory_adoption(profile=False)

    def test_failed_profile_open_after_mkdir_retains_root_and_quarantines_fixture(self):
        self.failed_directory_adoption(profile=True)

    def test_stalled_cleanup_helper_is_bounded_and_keeps_fixture_quarantined(self):
        task = self.task()
        entered = self.context.Value("i", 0, lock=False)

        def stall_cleanup(*args):
            entered.value = 1
            never_return()

        supervisor = self.supervisor(task, self.populated_worker)
        # The helper is persistent: install the fault before it forks during
        # allocation, not after the worker has already obtained its profile.
        with patch.object(profiles_module, "_cleanup_directories", new=stall_cleanup):
            result = self.run_bounded(supervisor)
        self.assertEqual(entered.value, 1, "cleanup-helper branch was not reached")
        self.assertEqual(result["worker_cleanup"], "closed")
        self.assertEqual(result["profile_cleanup"]["status"], "unknown")
        self.assertEqual(result["cleanup"], "unknown")
        self.assert_browser_data_retained()
        self.assert_quarantined(task)

    def test_crashed_cleanup_helper_cannot_release_fixture_from_worker_exit_alone(self):
        task = self.task()
        entered = self.context.Value("i", 0, lock=False)

        def crash_cleanup(*args):
            entered.value = 1
            os._exit(17)

        supervisor = self.supervisor(task, self.populated_worker)
        with patch.object(profiles_module, "_cleanup_directories", new=crash_cleanup):
            result = self.run_bounded(supervisor)
        self.assertEqual(entered.value, 1, "cleanup-crash branch was not reached")
        self.assertEqual(result["worker_cleanup"], "closed")
        self.assertEqual(result["profile_cleanup"]["status"], "unknown")
        self.assertEqual(result["cleanup"], "unknown")
        self.assert_browser_data_retained()
        self.assert_quarantined(task)


if __name__ == "__main__":
    unittest.main()
