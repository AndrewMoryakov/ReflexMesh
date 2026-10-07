"""Linux/WSL worker ownership and bounded, best-effort process-group cleanup.

The unreaped direct child pins its PID (and therefore its session/PGID) until
the last destructive signal. Do not register it with multiprocessing: starting
another multiprocessing child can implicitly reap an exited worker. Embedders
must leave this child's reaping to OwnedWorker; an external waitpid/SIGCHLD
reaper is unsupported. Strategies must not rely on multiprocessing's child
registry/current_process metadata: this worker does not use Process._bootstrap.
This is not a sandbox for descendants that escape the owned group/session,
nor an OS real-time termination guarantee. If a worker survives the entire
cleanup deadline, unknown can leave it running or awaiting reaping; the CLI's
exit reparents it to the OS reaper. Long-lived embedders must retain ownership
and arrange eventual reaping of such an unknown worker.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

POLL_SECONDS = 0.02


def _process_stat(pid):
    """Read identity/group fields; comm may itself contain spaces and ')'."""
    text = Path(f"/proc/{pid}/stat").read_text()
    fields = text[text.rfind(")") + 2:].split()
    return fields[0], int(fields[1]), int(fields[2]), int(fields[3]), int(fields[19])


class OwnedWorker:
    """A forked worker whose exit can be observed without releasing its PID."""

    def __init__(self, context, target, args):
        self.target, self.args = target, args
        self.ready = context.Value("i", 0, lock=False)
        self.pid = None
        self._identity = None
        self._released = False
        self._lost = False

    def start(self):
        if self.pid is not None:
            raise RuntimeError("worker already started")
        if (not hasattr(os, "WNOWAIT") or not Path("/proc/self/stat").exists() or
                signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL):
            raise RuntimeError("worker requires Linux procfs and exclusive child reaping")
        self.pid = os.fork()
        if self.pid == 0:
            code = 1
            try:
                os.setsid()
                # No strategy or browser startup before private-session setup.
                self.ready.value = 1
                self.target(*self.args)
                code = 0
            finally:
                os._exit(code)
        try:
            self._identity = _process_stat(self.pid)[4]
        except (OSError, ValueError, IndexError):
            self._lost = True

    def _exit_state(self):
        if self.pid is None or self._released or self._lost:
            return None
        try:
            info = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            return info is not None
        except (ChildProcessError, OSError):
            self._lost = True
            return None

    def is_alive(self):
        # Unknown must not shorten the supervisor's existing grace period.
        return not self._released and self._exit_state() is not True

    def _owns_group(self):
        if (not self.ready.value or self.pid is None or self.pid <= 1 or
                self.pid in (os.getpid(), os.getpgrp()) or self._exit_state() is None or
                signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL):
            return False
        try:
            _, parent, group, session, identity = _process_stat(self.pid)
            return (parent == os.getpid() and group == session == self.pid and
                    identity == self._identity)
        except (OSError, ValueError, IndexError):
            return False

    def _group_state(self, deadline):
        """True: live members; False: no live members; None: unproven.

        The pinned leader may be a zombie, which killpg(pgid, 0) would count as
        a member. Inspect procfs instead, and never equate leader exit with
        cleanup. A member observed outside the owned group makes it unknown.
        """
        if not self._owns_group():
            return None
        live = False
        try:
            with os.scandir("/proc") as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        return None
                    if not entry.name.isdecimal():
                        continue
                    try:
                        state, _, group, session, _ = _process_stat(int(entry.name))
                    except FileNotFoundError:
                        continue  # A process disappearing cannot remain live.
                    if state in ("Z", "X", "x"):
                        continue
                    if session == self.pid and group != self.pid:
                        return None
                    if group == self.pid:
                        if session != self.pid:
                            return None
                        live = True
        except (OSError, ValueError, IndexError):
            return None
        # A procfs scan is not atomic (members can fork and disappear). Its
        # result is only a hint; success also requires post-reap group absence.
        if not self._owns_group():
            return None
        return live or self._exit_state() is not True

    def _signal_group(self, sig):
        if not self._owns_group():
            return False
        try:
            os.killpg(self.pid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            # Neither a delivery attempt nor ESRCH proves complete cleanup.
            return False

    def _wait_group(self, deadline):
        while time.monotonic() < deadline:
            state = self._group_state(deadline)
            if state is False:
                return False
            if state is None:
                self._uncertain = True
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
        return None

    def _release(self):
        """Nonblocking reap, only after the last possible group signal."""
        if self._exit_state() is not True:
            return False
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            self._lost = True
            return False
        self._released = pid == self.pid
        return self._released

    def _confirm_absent(self, deadline):
        """Existence-only probes after reaping; never signal a reused PGID.

        Signal 0 delivers no signal. A reused ID or orphaned zombies can keep
        this conservatively unknown, but can never cause another TERM/KILL.
        """
        if not self._released:
            return False
        while time.monotonic() < deadline:
            try:
                os.killpg(self.pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
        return False

    def _release_until(self, deadline):
        while time.monotonic() < deadline:
            if self._release():
                return True
            if self._lost:
                return False
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
        return False

    def cleanup(self, allowance):
        """Use at most allowance for TERM + KILL waits/probes, excluding OS delay.

        Split the single termination budget evenly between TERM and KILL.
        Never release the PID before escalation, even when the worker exited.
        Unknown observations, surviving members and unproven ownership cannot
        produce closed/forced. A post-reap ESRCH is required for success;
        orphaned zombies still awaiting their OS reaper can yield unknown.
        """
        start = time.monotonic()
        deadline = start + max(0, allowance)
        term_deadline = start + max(0, allowance) / 2
        state = self._group_state(deadline)
        self._uncertain = state is None
        if state is False:
            return "closed" if self._release() and self._confirm_absent(deadline) else "unknown"
        if time.monotonic() >= deadline or not self._signal_group(signal.SIGTERM):
            self._release()
            return "unknown"
        self._wait_group(term_deadline)
        # The leader may now be dead while descendants remain. Keep the pin
        # through KILL even if a non-atomic census happens to look empty.
        if time.monotonic() >= deadline or not self._signal_group(signal.SIGKILL):
            self._release()
            return "unknown"
        # Leave time for reaping and the final authoritative absence probe.
        self._wait_group(start + max(0, allowance) * 0.75)
        reaped = self._release_until(deadline)
        absent = reaped and self._confirm_absent(deadline)
        return "forced" if absent and not self._uncertain else "unknown"
