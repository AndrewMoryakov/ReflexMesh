"""Process-group regressions. Added as source; execution is a separate check."""

import errno
import multiprocessing as mp
import os
import signal
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.runtime import process_group as groups
from reflexmesh.runtime.process_group import OwnedWorker


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def fake_worker():
    worker = OwnedWorker.__new__(OwnedWorker)
    worker.pid = 70001
    worker.ready = SimpleNamespace(value=1)
    worker._identity = 123
    worker._released = worker._lost = False
    return worker


class CleanupUnitTests(unittest.TestCase):
    def test_dead_leader_with_live_descendants_still_escalates(self):
        worker, clock, signals = fake_worker(), Clock(), []

        def send(sig):
            self.assertFalse(worker._released)
            signals.append(sig)
            return True

        def release():
            self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
            worker._released = True
            return True

        with patch.object(worker, "is_alive", return_value=False), \
                patch.object(worker, "_group_state", side_effect=lambda _: signal.SIGKILL not in signals), \
                patch.object(worker, "_signal_group", side_effect=send), \
                patch.object(worker, "_release", side_effect=release), \
                patch.object(groups.os, "killpg", side_effect=ProcessLookupError), \
                patch.object(groups.time, "monotonic", clock.monotonic), \
                patch.object(groups.time, "sleep", clock.sleep):
            self.assertEqual(worker.cleanup(2), "forced")
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertLessEqual(clock.now, 2)

    def test_term_kills_leader_but_does_not_skip_kill(self):
        worker, signals = fake_worker(), []
        with patch.object(worker, "_group_state", side_effect=[True, False, False]), \
                patch.object(worker, "_signal_group", side_effect=lambda sig: signals.append(sig) or True), \
                patch.object(worker, "_release", return_value=True), \
                patch.object(worker, "_confirm_absent", return_value=True):
            self.assertEqual(worker.cleanup(2), "forced")
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])

    def test_natural_exit_requires_absence_after_reap(self):
        worker = fake_worker()
        with patch.object(worker, "_group_state", return_value=False), \
                patch.object(worker, "_release", return_value=True), \
                patch.object(worker, "_confirm_absent", return_value=True) as absent, \
                patch.object(worker, "_signal_group") as send:
            self.assertEqual(worker.cleanup(2), "closed")
        absent.assert_called_once()
        send.assert_not_called()

    def test_fork_race_after_empty_scan_cannot_claim_closed(self):
        worker = fake_worker()
        with patch.object(worker, "_group_state", return_value=False), \
                patch.object(worker, "_release", return_value=True), \
                patch.object(worker, "_confirm_absent", return_value=False), \
                patch.object(worker, "_signal_group") as send:
            self.assertEqual(worker.cleanup(2), "unknown")
        send.assert_not_called()

    def test_unproven_group_does_not_receive_destructive_signals(self):
        worker = fake_worker()
        with patch.object(worker, "_owns_group", return_value=False), \
                patch.object(worker, "_release", return_value=False), \
                patch.object(groups.os, "killpg") as send:
            self.assertEqual(worker.cleanup(2), "unknown")
        send.assert_not_called()

    def test_identity_and_session_safety(self):
        invalid = [
            (0, 70001, 1, 70001, 70001, 123),  # no setup acknowledgement
            (1, 0, 1, 0, 0, 123),
            (1, 1, 1, 1, 1, 123),
            (1, 42, 42, 42, 42, 123),  # supervisor PID/group
            (1, 70001, 9, 70001, 70001, 123),  # not our direct child
            (1, 70001, 42, 70002, 70001, 123),  # foreign group
            (1, 70001, 42, 70001, 70002, 123),  # foreign session
            (1, 70001, 42, 70001, 70001, 124),  # reused PID identity
        ]
        for ready, pid, parent, group, session, identity in invalid:
            with self.subTest(pid=pid, ready=ready, parent=parent, group=group, session=session):
                worker = fake_worker()
                worker.pid, worker.ready.value = pid, ready
                with patch.object(worker, "_exit_state", return_value=True), \
                        patch.object(groups.os, "getpid", return_value=42), \
                        patch.object(groups.os, "getpgrp", return_value=42), \
                        patch.object(groups.signal, "getsignal", return_value=signal.SIG_DFL), \
                        patch.object(groups, "_process_stat", return_value=("Z", parent, group, session, identity)), \
                        patch.object(groups.os, "killpg") as send:
                    self.assertFalse(worker._signal_group(signal.SIGKILL))
                send.assert_not_called()

    def test_exited_child_is_observed_without_reaping(self):
        worker = fake_worker()
        with patch.object(groups.os, "waitid", return_value=SimpleNamespace(si_pid=worker.pid)) as observe, \
                patch.object(groups.os, "waitpid") as reap:
            self.assertFalse(worker.is_alive())
            self.assertFalse(worker.is_alive())
        self.assertEqual(observe.call_count, 2)
        self.assertTrue(observe.call_args.args[2] & os.WNOWAIT)
        reap.assert_not_called()

    def test_lost_child_ownership_never_signals_reused_pid(self):
        worker = fake_worker()
        with patch.object(groups.os, "waitid", side_effect=ChildProcessError), \
                patch.object(groups.os, "killpg") as send:
            self.assertFalse(worker._signal_group(signal.SIGTERM))
            self.assertTrue(worker._lost)
        send.assert_not_called()

    def test_ownership_loss_before_escalation_is_unknown(self):
        worker, clock = fake_worker(), Clock()
        with patch.object(worker, "_group_state", return_value=True), \
                patch.object(worker, "_signal_group", side_effect=[True, False]) as send, \
                patch.object(worker, "_release", return_value=False), \
                patch.object(groups.time, "monotonic", clock.monotonic), \
                patch.object(groups.time, "sleep", clock.sleep):
            self.assertEqual(worker.cleanup(2), "unknown")
        self.assertEqual(send.call_count, 2)

    def test_surviving_or_unobservable_group_exhausts_one_budget(self):
        for state in (True, None):
            with self.subTest(state=state):
                worker, clock = fake_worker(), Clock()
                with patch.object(worker, "_group_state", return_value=state), \
                        patch.object(worker, "_signal_group", return_value=True), \
                        patch.object(worker, "_release", return_value=False), \
                        patch.object(groups.time, "monotonic", clock.monotonic), \
                        patch.object(groups.time, "sleep", clock.sleep):
                    self.assertEqual(worker.cleanup(2), "unknown")
                self.assertLessEqual(clock.now, 2)

    def test_reused_group_after_reap_only_gets_signal_zero(self):
        worker, clock = fake_worker(), Clock()
        worker._released = True
        with patch.object(groups.os, "killpg") as probe, \
                patch.object(groups.time, "monotonic", clock.monotonic), \
                patch.object(groups.time, "sleep", clock.sleep):
            self.assertFalse(worker._confirm_absent(0.1))
        self.assertTrue(probe.call_args_list)
        self.assertTrue(all(call.args == (worker.pid, 0) for call in probe.call_args_list))
        self.assertLessEqual(clock.now, 0.1)

    def test_uncertainty_is_sticky_even_when_original_group_disappears(self):
        # None can represent an observed same-session escaped subgroup, not
        # just an incomplete scan. Absence of the original group cannot erase it.
        for states in ([None, False, False], [True, None, False, False]):
            with self.subTest(states=states):
                worker, clock = fake_worker(), Clock()
                with patch.object(worker, "_group_state", side_effect=states), \
                        patch.object(worker, "_signal_group", return_value=True), \
                        patch.object(worker, "_release", return_value=True), \
                        patch.object(worker, "_confirm_absent", return_value=True), \
                        patch.object(groups.time, "monotonic", clock.monotonic), \
                        patch.object(groups.time, "sleep", clock.sleep):
                    self.assertEqual(worker.cleanup(2), "unknown")

    def test_slow_leader_reaping_uses_remaining_budget(self):
        worker, clock = fake_worker(), Clock()
        with patch.object(worker, "_group_state", side_effect=[True, False, False]), \
                patch.object(worker, "_signal_group", return_value=True), \
                patch.object(worker, "_release", side_effect=[False, False, True]) as reap, \
                patch.object(worker, "_confirm_absent", return_value=True), \
                patch.object(groups.time, "monotonic", clock.monotonic), \
                patch.object(groups.time, "sleep", clock.sleep):
            self.assertEqual(worker.cleanup(2), "forced")
        self.assertEqual(reap.call_count, 3)
        self.assertLessEqual(clock.now, 2)

    def test_permission_failure_is_not_absence(self):
        worker = fake_worker()
        worker._released = True
        with patch.object(groups.os, "killpg", side_effect=PermissionError(errno.EPERM, "denied")):
            self.assertFalse(worker._confirm_absent(time.monotonic() + 1))

    def test_no_signals_after_budget_is_exhausted(self):
        worker = fake_worker()
        with patch.object(worker, "_group_state", return_value=None), \
                patch.object(worker, "_signal_group") as send, \
                patch.object(worker, "_release", return_value=False):
            self.assertEqual(worker.cleanup(0), "unknown")
        send.assert_not_called()

    def test_proc_stat_parser_handles_parentheses_in_command(self):
        fields = ["Z", "42", "70001", "70001"] + ["0"] * 15 + ["123"]
        with patch.object(groups.Path, "read_text", return_value="70001 (a tricky ) name) " + " ".join(fields)):
            self.assertEqual(groups._process_stat(70001), ("Z", 42, 70001, 70001, 123))

    def test_failed_setsid_never_runs_strategy(self):
        worker = OwnedWorker(mp.get_context("fork"), Mock(), ())
        with patch.object(groups.os, "fork", return_value=0), \
                patch.object(groups.os, "setsid", side_effect=OSError("setup failed")), \
                patch.object(groups.os, "_exit", side_effect=SystemExit), \
                patch.object(groups.signal, "getsignal", return_value=signal.SIG_DFL), \
                patch.object(groups.Path, "exists", return_value=True):
            with self.assertRaises(SystemExit):
                worker.start()
        self.assertEqual(worker.ready.value, 0)
        worker.target.assert_not_called()

    def test_fixture_release_polling_accepts_a_lock_free_flag(self):
        clock, release = Clock(), SimpleNamespace(value=0)

        def release_after_poll(seconds):
            clock.sleep(seconds)
            release.value = 1

        # The plain flag deliberately has no Event.wait/set or Condition
        # bookkeeping that can be stranded when a waiting worker is killed.
        with patch.object(os, "fork", return_value=70002), \
                patch.object(time, "monotonic", clock.monotonic), \
                patch.object(time, "sleep", side_effect=release_after_poll):
            orphan_descendant(None, release, None)
        self.assertEqual(release.value, 1)
        self.assertLessEqual(clock.now, 0.02)

    def test_fixture_release_polling_stops_at_its_deadline(self):
        clock, release = Clock(), SimpleNamespace(value=0)
        with patch.object(os, "fork", return_value=70002), \
                patch.object(time, "monotonic", clock.monotonic), \
                patch.object(time, "sleep", clock.sleep):
            orphan_descendant(None, release, None)
        self.assertEqual(release.value, 0)
        self.assertEqual(clock.now, 5)


def orphan_descendant(ready, release, child_pid):
    pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child_pid.value = os.getpid()
        ready.set()
        while True:
            time.sleep(0.02)
    deadline = time.monotonic() + 5
    while not release.value and time.monotonic() < deadline:
        time.sleep(min(0.02, max(0, deadline - time.monotonic())))


@unittest.skipUnless(sys.platform.startswith("linux") and hasattr(os, "pidfd_open"),
                     "requires Linux procfs, WNOWAIT and pidfd cleanup")
class CleanupProcessTests(unittest.TestCase):
    def test_dead_worker_live_term_resistant_descendant_is_killed(self):
        self._exercise_cleanup(leader_dead=True)

    def test_term_exits_worker_but_kill_still_reaches_descendant(self):
        self._exercise_cleanup(leader_dead=False)

    def _exercise_cleanup(self, *, leader_dead):
        ctx = mp.get_context("fork")
        ready = ctx.Event()
        # Event.set() can deadlock after TERM kills its Condition waiter.
        # A lock-free flag is safe to publish even after that worker is gone.
        release = ctx.Value("b", 0, lock=False)
        child_pid = ctx.Value("i", 0)
        worker = OwnedWorker(ctx, orphan_descendant, (ready, release, child_pid))
        pidfd = None
        worker.start()
        try:
            self.assertTrue(ready.wait(2))
            self.assertGreater(child_pid.value, 0)
            pidfd = os.pidfd_open(child_pid.value)
            if leader_dead:
                release.value = 1
                deadline = time.monotonic() + 2
                while worker.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(worker.is_alive())
            else:
                self.assertTrue(worker.is_alive())
            self.assertTrue(worker._owns_group())
            self.assertNotIn(groups._process_stat(child_pid.value)[0], ("Z", "X", "x"))
            started = time.monotonic()
            with patch.object(groups.os, "killpg", wraps=os.killpg) as send:
                result = worker.cleanup(2)
            destructive = [call.args[1] for call in send.call_args_list if call.args[1] != 0]
            self.assertEqual(destructive, [signal.SIGTERM, signal.SIGKILL])
            self.assertLess(time.monotonic() - started, 3)
            # Orphan zombies may await PID 1's reaper; never turn that into an
            # unconditional 'forced' assertion. The child must not be live.
            self.assertIn(result, ("forced", "unknown"))
            try:
                self.assertIn(groups._process_stat(child_pid.value)[0], ("Z", "X", "x"))
            except FileNotFoundError:
                pass
        finally:
            release.value = 1
            if pidfd is not None:
                try:
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.close(pidfd)
            if not worker._released:
                worker.cleanup(1)

    def test_verifier_start_does_not_reap_dead_worker_pin(self):
        worker = OwnedWorker(mp.get_context("fork"), lambda: None, ())
        worker.start()
        other = None
        try:
            deadline = time.monotonic() + 2
            while worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(worker.is_alive())
            other = mp.get_context("fork").Process(target=lambda: None)
            other.start()  # multiprocessing's global _cleanup must not own worker.
            other.join(1)
            self.assertTrue(worker._owns_group())
            self.assertEqual(worker.cleanup(1), "closed")
        finally:
            if other is not None:
                if other.is_alive():
                    other.kill()
                other.join(1)
            if not worker._released:
                worker.cleanup(1)


if __name__ == "__main__":
    unittest.main()
