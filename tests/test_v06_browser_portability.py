"""The pieces of the browser path that differ between Linux and Windows."""

import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reflexmesh.runtime.browser_run import _kill, _pid_alive, processes_using  # noqa: E402
from reflexmesh.tracing import slot_evidence  # noqa: E402


class Processes(unittest.TestCase):
    def child(self):
        marker = "reflexmesh-portability-" + uuid.uuid4().hex
        proc = subprocess.Popen([sys.executable, "-c", "import time, sys; time.sleep(60)", marker])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        return proc, marker

    def test_asking_whether_a_process_lives_does_not_end_it(self):
        # On Windows os.kill(pid, 0) is TerminateProcess, not a probe.
        proc, _ = self.child()
        self.assertTrue(_pid_alive(proc.pid))
        time.sleep(0.3)
        self.assertIsNone(proc.poll())
        self.assertTrue(_pid_alive(proc.pid))

    def test_a_process_is_found_by_its_command_line_and_killed(self):
        proc, marker = self.child()
        deadline = time.monotonic() + 5
        while proc.pid not in processes_using(marker) and time.monotonic() < deadline:
            time.sleep(0.05)
        found = processes_using(marker)
        # A Windows venv python.exe is a launcher with the real interpreter as its child: both carry the marker,
        # and both are killed, as the attempt's cleanup does with a browser's process tree.
        self.assertIn(proc.pid, found)
        for pid in found:
            _kill(pid)
        proc.wait(10)
        deadline = time.monotonic() + 5
        while processes_using(marker) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_pid_alive(proc.pid))
        self.assertEqual(processes_using(marker), [])


class InProcessDatagrams(unittest.TestCase):
    """The ledger's datagram pair where AF_UNIX is missing."""

    def setUp(self):
        queue, lock, state = slot_evidence.deque(), slot_evidence.threading.Lock(), {"closed": False}
        self.receiver = slot_evidence._InProcessDatagram(queue, lock, state)
        self.sender = slot_evidence._InProcessDatagram(queue, lock, state)

    def test_packets_arrive_whole_and_in_order(self):
        self.assertEqual(self.sender.send(b"one"), 3)
        self.sender.send(b"two")
        self.assertEqual(self.receiver.recvmsg(100)[0], b"one")
        packet, _, flags, _ = self.receiver.recvmsg(100)
        self.assertEqual((packet, flags), (b"two", 0))
        with self.assertRaises(BlockingIOError):
            self.receiver.recvmsg(100)

    def test_full_closed_and_truncated(self):
        self.sender.send(b"x" * 10)
        self.assertNotEqual(self.receiver.recvmsg(4)[2] & slot_evidence.socket.MSG_TRUNC, 0)
        for _ in range(slot_evidence._InProcessDatagram.CAPACITY):
            self.sender.send(b"x")
        with self.assertRaises(BlockingIOError):
            self.sender.send(b"x")
        self.sender.close()
        with self.assertRaises(OSError):
            self.sender.send(b"x")


if __name__ == "__main__":
    unittest.main()
