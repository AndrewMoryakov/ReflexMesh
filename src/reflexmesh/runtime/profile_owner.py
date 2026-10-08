"""Lazy supervisor-owned browser profiles and bounded, descriptor-relative cleanup.

Only a supervisor-owned trusted keeper creates directories. The browser worker
requests one bounded private receipt; it never registers a cleanup path. The
supervisor publishes the receipt only after its keeper holds directory identities
and descriptors. Lost handoffs and partially started browser backends therefore
do not lose cleanup ownership. Even allocation runs outside the supervisor, so
a stalled temporary-directory filesystem cannot stall its cancellation loop.

Deletion requires confirmed absence of the owned worker group. Traversal uses
retained descriptors, never a checked pathname followed by pathname rmtree.
No-follow opens and Linux mount IDs reject symlink and bind-mount traversal.
This is not isolation against arbitrary concurrent same-user Python: like the
worker-group boundary, it assumes no unrelated process maliciously races local
directory entries. Linux has no inode-conditional rmdir; names are rechecked
immediately before descriptor-relative empty-directory removal.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from reflexmesh.runtime.process_group import OwnedWorker

RECEIPT_BYTES = 4096
POLL_SECONDS = 0.01
STOP_RESERVE_SECONDS = 0.25
MAX_ENTRIES = 100_000
MAX_DEPTH = 64
DIRECTORY_FLAGS = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                   if all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"))
                   else None)


class ProfileUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("browser_profile_unavailable")


@dataclass(frozen=True, repr=False)
class ProfileLease:
    path: Path
    profile_id: str
    device: int
    inode: int

    def __repr__(self):
        return "ProfileLease(<private profile capability>)"


class ProfileClient:
    """One-use bootstrap capability; fixed-size single-writer private receipt."""

    def __init__(self, context, profile_id):
        self.requested = context.Value("i", 0, lock=False)
        self.ready = context.Value("i", 0, lock=False)
        self._claimed = context.Value("i", 0, lock=False)
        self._path = context.Array("B", RECEIPT_BYTES, lock=False)
        self._device = context.Value("Q", 0, lock=False)
        self._inode = context.Value("Q", 0, lock=False)
        self._profile_id = profile_id
        self._gate = None
        self._owner = None
        self._identity = None
        self._used = False

    def bind_gate(self, gate):
        from reflexmesh.runtime.ownership import FixtureOwnership

        owner = getattr(gate, "fixture_owner", None)
        if (getattr(gate, "profile_client", None) is not self or
                type(owner) is not FixtureOwnership or
                (self._gate is not None and (self._gate is not gate or self._owner is not owner or
                                             self._identity != owner.identity_tuple))):
            raise ProfileUnavailable()
        self._gate = gate
        self._owner = owner
        self._identity = owner.identity_tuple

    def belongs_to(self, task, owner=None):
        return (self._gate is not None and self._gate.profile_client is self and
                self._gate.fixture_owner is self._owner and
                (owner is None or owner is self._owner) and
                self._owner.identity_tuple == self._identity and
                (self._identity[0], self._identity[1], self._identity[3]) ==
                (task.task_id, task.revision, task.run_id))

    def _admit(self):
        if self._gate is None:
            raise ProfileUnavailable()
        self._gate.remaining()

    def claim(self, timeout=2.0):
        if self._used:
            raise ProfileUnavailable()
        self._used = True
        self._admit()
        deadline = time.monotonic() + max(0, timeout)
        self._gate._acquire()
        try:
            self._gate._check()
            if self._claimed.value:
                raise ProfileUnavailable()
            self._claimed.value = 1
            self.requested.value = 1
        finally:
            self._gate.lock.release()
        while time.monotonic() < deadline:
            self._admit()
            state = self.ready.value
            if state == 1:
                lease = self._receipt()
                self._admit()  # Cancellation can win while receipt bytes are copied.
                return lease
            if state != 0:
                raise ProfileUnavailable()
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
        raise ProfileUnavailable()

    def _receipt(self):
        raw = bytes(self._path).split(b"\0", 1)[0]
        path = Path(os.fsdecode(raw))
        if (not raw or not path.is_absolute() or not self._inode.value or
                len(raw) >= RECEIPT_BYTES):
            raise ProfileUnavailable()
        return ProfileLease(path, self._profile_id, self._device.value, self._inode.value)


@dataclass(frozen=True)
class _Directory:
    name: str
    fd: int
    device: int
    inode: int
    mount_id: int


def _mount_id(fd):
    # Device IDs alone do not distinguish same-filesystem bind mounts.
    with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as source:
        lines = source.read(8192).splitlines()
    values = [line.split(":", 1)[1].strip() for line in lines if line.startswith("mnt_id:")]
    if len(values) != 1 or not values[0].isdecimal():
        raise ProfileUnavailable()
    return int(values[0])


def _directory(name, fd):
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
            stat.S_IMODE(info.st_mode) != 0o700):
        raise ProfileUnavailable()
    return _Directory(name, fd, info.st_dev, info.st_ino, _mount_id(fd))


def _same(info, device, inode):
    return info.st_dev == device and info.st_ino == inode


def _matches(parent_fd, directory):
    info = os.stat(directory.name, dir_fd=parent_fd, follow_symlinks=False)
    held = os.fstat(directory.fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
            stat.S_IMODE(info.st_mode) != 0o700 or
            not _same(info, directory.device, directory.inode) or
            not _same(held, directory.device, directory.inode) or
            _mount_id(directory.fd) != directory.mount_id):
        return False
    current = os.open(directory.name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        # A bind mount can preserve inode/device while changing resolution.
        # Compare a fresh no-follow name resolution with the retained handle.
        return (_same(os.fstat(current), directory.device, directory.inode) and
                _mount_id(current) == directory.mount_id)
    finally:
        os.close(current)


class _CleanupRefused(Exception):
    pass


class _Budget:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + max(0, seconds)
        self.entries = 0
        self.changed = False

    def check(self, depth=0):
        self.entries += 1
        if (time.monotonic() >= self.deadline or self.entries > MAX_ENTRIES or
                depth > MAX_DEPTH):
            raise TimeoutError


def _clear_directory(fd, device, mount_id, budget, depth=0):
    budget.check(depth)
    # scandir(fd) is incremental; never materialize a browser-sized list in
    # either the supervisor or cleanup helper.
    with os.scandir(fd) as entries:
        for entry in entries:
            budget.check(depth)
            info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
            if info.st_uid != os.geteuid():
                raise _CleanupRefused
            if stat.S_ISDIR(info.st_mode):
                child = os.open(entry.name, DIRECTORY_FLAGS, dir_fd=fd)
                try:
                    held = os.fstat(child)
                    if (not _same(held, info.st_dev, info.st_ino) or held.st_dev != device or
                            _mount_id(child) != mount_id):
                        raise _CleanupRefused
                    _clear_directory(child, device, mount_id, budget, depth + 1)
                    budget.check(depth)
                    current = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    if not stat.S_ISDIR(current.st_mode) or not _same(current, held.st_dev, held.st_ino):
                        raise _CleanupRefused
                    os.rmdir(entry.name, dir_fd=fd)
                    budget.changed = True
                finally:
                    os.close(child)
            else:
                # unlink never follows symlinks. A link to foreign data is
                # removed only as an entry inside the owned profile.
                if info.st_dev != device:
                    raise _CleanupRefused
                os.unlink(entry.name, dir_fd=fd)
                budget.changed = True


def _cleanup_directories(parent_fd, anchor, profile, result, seconds):
    """Helper only: 1 removed, 2 retained identity/boundary, 3 incomplete."""
    budget = _Budget(seconds)
    try:
        budget.check()
        if not _matches(parent_fd, anchor):
            raise _CleanupRefused
        if profile is not None:
            if (not _matches(anchor.fd, profile) or profile.mount_id != anchor.mount_id or
                    profile.device != anchor.device):
                raise _CleanupRefused
            _clear_directory(profile.fd, profile.device, profile.mount_id, budget)
            budget.check()
            if not _matches(parent_fd, anchor) or not _matches(anchor.fd, profile):
                raise _CleanupRefused
            os.rmdir(profile.name, dir_fd=anchor.fd)
            budget.changed = True
        budget.check()
        if not _matches(parent_fd, anchor):
            raise _CleanupRefused
        # Only the owned profile was traversed. Never recursively clean an
        # anchor that unexpectedly contains some other file or directory.
        os.rmdir(anchor.name, dir_fd=parent_fd)
        result.value = 1
    except (FileNotFoundError, _CleanupRefused):
        result.value = 3 if budget.changed else 2
    except BaseException:
        # Interrupted/partial deletion is not proof of full retention/removal.
        result.value = 3


@dataclass
class _KeeperControl:
    prepared: object
    creation_intent: object
    command: object
    result: object
    cleanup_deadline: object


class AttemptProfiles:
    """Supervisor owns a lazy keeper, its retained fds and terminal cleanup."""

    def __init__(self, context):
        self.client = ProfileClient(context, uuid.uuid4().hex)
        self._context = context
        self._supervisor_pid = os.getpid()
        self._closed = self._created = False
        self._allocation_incomplete = False
        self._anchor_name = "reflexmesh-attempt-" + uuid.uuid4().hex
        self._profile_name = "browser-use-user-data-dir-reflexmesh-" + uuid.uuid4().hex
        self._parent_fd = None
        self._anchor = self._profile = None
        self._fds = []
        self._lease = None
        self._cleanup_result = None
        self._keeper = self._control = None

    def _own_fd(self, fd):
        self._fds.append(fd)
        return fd

    def service(self):
        if os.getpid() != self._supervisor_pid:
            raise ProfileUnavailable()
        if self._closed or self.client.ready.value != 0 or not self.client.requested.value:
            return
        try:
            self.client._admit()
            if self._keeper is None:
                # Created only after the ordinary worker has forked: it gets
                # allocation/receipt memory, never the keeper's delete command.
                value = self._context.Value
                self._control = _KeeperControl(value("i", 0, lock=False), value("i", 0, lock=False),
                                               value("i", 0, lock=False), value("i", 0, lock=False),
                                               value("d", 0.0, lock=False))
                self._keeper = OwnedWorker(self._context, self._keep_profile, ())
                self._keeper.start()
            if self._control.prepared.value == 1:
                if self._keeper._exit_state() is not False or not self._keeper._owns_group():
                    raise ProfileUnavailable()
                self._lease = self.client._receipt()
                self.client._admit()
                # Supervisor adoption precedes publication to the browser.
                self.client.ready.value = 1
            elif self._control.prepared.value == -1 or not self._keeper.is_alive():
                raise ProfileUnavailable()
        except BaseException:
            self.client.ready.value = -1

    def _allocate(self):
        """Trusted keeper only. Parent-chosen names; no browser-supplied path."""
        if DIRECTORY_FLAGS is None:
            raise ProfileUnavailable()
        self.client._admit()
        try:
            base = Path(tempfile.gettempdir()).resolve(strict=True)
            self._parent_fd = self._own_fd(os.open(base, DIRECTORY_FLAGS))
            anchor_name = self._anchor_name
            self._control.creation_intent.value = 1
            # Record creation before any fallible open, stat or publication.
            os.mkdir(anchor_name, mode=0o700, dir_fd=self._parent_fd)
            self._created = self._allocation_incomplete = True
            fd = self._own_fd(os.open(anchor_name, DIRECTORY_FLAGS, dir_fd=self._parent_fd))
            self._anchor = _directory(anchor_name, fd)
            self._allocation_incomplete = False
            name = self._profile_name
            os.mkdir(name, mode=0o700, dir_fd=fd)
            self._allocation_incomplete = True
            profile_fd = self._own_fd(os.open(name, DIRECTORY_FLAGS, dir_fd=fd))
            self._profile = _directory(name, profile_fd)
            self._allocation_incomplete = False
            if self._profile.mount_id != self._anchor.mount_id:
                raise ProfileUnavailable()
            path = os.fsencode(base / anchor_name / name)
            if len(path) >= RECEIPT_BYTES or b"\0" in path:
                raise ProfileUnavailable()
            self.client._admit()
            self.client._path[:len(path)] = path
            self.client._device.value = self._profile.device
            self.client._inode.value = self._profile.inode
        except BaseException:
            raise ProfileUnavailable() from None

    def _keep_profile(self):
        """Hold exact fds until the parent explicitly authorizes cleanup."""
        control = self._control
        try:
            try:
                self._allocate()
                control.prepared.value = 1
            except BaseException:
                control.prepared.value = -1
            while not control.command.value:
                if os.getppid() != self._supervisor_pid:
                    # Parent loss is not a task lifetime limit and never
                    # authorizes deletion while a browser may still be live.
                    return
                time.sleep(POLL_SECONDS)
            if control.command.value != 1:
                return  # Explicit retention; never touch browser data.
            if not self._created:
                control.result.value = 3 if control.creation_intent.value else 4
            elif self._allocation_incomplete or self._anchor is None:
                control.result.value = 3
            else:
                _cleanup_directories(self._parent_fd, self._anchor, self._profile, control.result,
                                     max(0, control.cleanup_deadline.value - time.monotonic()))
        finally:
            for fd in self._fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def close_requests(self):
        self._closed = True
        if self.client.ready.value == 0:
            self.client.ready.value = -1

    def _finish(self, status, reason):
        self._cleanup_result = {"status": status, "reason": reason}
        return dict(self._cleanup_result)

    def cleanup(self, process_cleanup, allowance=2.0):
        """One bounded budget including helper stop; never traverse in parent.

        An unknown keeper is retained for eventual reaping by a long-lived
        embedder, just like an unknown primary OwnedWorker. No cleanup retry or
        fixture reuse is authorized by an incomplete result.
        """
        if os.getpid() != self._supervisor_pid:
            raise ProfileUnavailable()
        if self._cleanup_result is not None:
            return dict(self._cleanup_result)
        self.close_requests()
        if self._keeper is None or self._keeper.pid is None:
            return self._finish("not_created", "bootstrap_not_allocated")
        deadline = time.monotonic() + max(0, allowance)
        control, helper = self._control, self._keeper
        control.cleanup_deadline.value = max(time.monotonic(), deadline - STOP_RESERVE_SECONDS)
        control.command.value = 1 if process_cleanup in ("closed", "forced") else 2
        try:
            work_deadline = deadline - STOP_RESERVE_SECONDS
            while helper.is_alive() and time.monotonic() < work_deadline:
                time.sleep(min(POLL_SECONDS, max(0, work_deadline - time.monotonic())))
            stopped = helper.cleanup(max(0, deadline - time.monotonic()))
        except BaseException:
            # start() may have created a child before encountering an error.
            if helper.pid is not None:
                try:
                    helper.cleanup(max(0, deadline - time.monotonic()))
                except BaseException:
                    pass
            return self._finish("unknown", "cleanup_helper_failed")
        if stopped not in ("closed", "forced"):
            return self._finish("unknown", "cleanup_helper_unknown")
        if control.result.value == 1:
            return self._finish("removed", "owned_profile_removed")
        if control.result.value == 4 or not control.creation_intent.value:
            return self._finish("not_created", "bootstrap_not_allocated")
        if process_cleanup not in ("closed", "forced"):
            return self._finish("retained", "worker_cleanup_unknown")
        if control.result.value == 2:
            return self._finish("retained", "profile_identity_or_boundary_changed")
        return self._finish("unknown", "cleanup_incomplete")
