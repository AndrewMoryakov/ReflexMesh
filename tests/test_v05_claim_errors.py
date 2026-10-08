"""Real HTTP claim-error regressions through the client and execution CLI.

These tests stop during fixture preflight, before browser or model startup.
They exercise the fixture server's actual error envelopes, not a patched claim
helper or a synthetic OwnershipError supplied directly to the strategy.
"""

import copy
import json
import multiprocessing as mp
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reflexmesh.runtime.ownership import FixtureOwnership, OwnershipError
from reflexmesh.verification.verifier import read_fixture
from test_v05_fixture import FixtureServerCase
from test_v05_runtime import sample


HAS_FORK = "fork" in mp.get_all_start_methods()
LINUX_OWNED_WORKER = sys.platform.startswith("linux") and hasattr(os, "WNOWAIT") and HAS_FORK


@unittest.skipUnless(HAS_FORK, "fixture ownership uses fork-compatible shared state")
class ClaimClientErrors(FixtureServerCase):
    def owner(self, task, attempt):
        return FixtureOwnership(mp.get_context("fork"), task, attempt)

    def test_wrong_run_id_from_real_server_is_fixture_mismatch(self):
        task = self.task()
        owner = self.owner(replace(task, run_id="wrong-fixture-run"), "wrong-run-attempt")
        with self.assertRaises(OwnershipError) as error:
            owner.acquire(1)
        self.assertEqual(error.exception.reason, "fixture_mismatch")
        self.assertEqual(owner.acquisition_attempted.value, 1)
        self.assertIsNone(owner.binding)
        self.assertFalse(owner.export())
        current = read_fixture(task, 1)
        self.assertEqual(current["phase"], "unclaimed")
        self.assertEqual(current["sequence"], 0)

    def test_true_contention_from_real_server_is_fixture_busy(self):
        task = self.task()
        first = self.owner(task, "first-owner")
        first.acquire(1)
        second = self.owner(task, "contending-owner")
        with self.assertRaises(OwnershipError) as error:
            second.acquire(1)
        self.assertEqual(error.exception.reason, "fixture_busy")
        self.assertIsNone(second.binding)
        self.assertFalse(second.export())
        current = read_fixture(task, 1)
        self.assertEqual(current["phase"], "active")
        self.assertEqual(current["binding"], first.binding)
        self.assertEqual(current["sequence"], 0)

    def test_mismatch_takes_precedence_even_when_actual_fixture_is_busy(self):
        task = self.task()
        first = self.owner(task, "first-owner")
        first.acquire(1)
        wrong = self.owner(replace(task, run_id="wrong-fixture-run"), "wrong-run-attempt")
        with self.assertRaises(OwnershipError) as error:
            wrong.acquire(1)
        self.assertEqual(error.exception.reason, "fixture_mismatch")
        current = read_fixture(task, 1)
        self.assertEqual(current["phase"], "active")
        self.assertEqual(current["binding"], first.binding)


@unittest.skipUnless(LINUX_OWNED_WORKER, "execution CLI requires Linux owned workers")
class ClaimCliErrors(FixtureServerCase):
    def run_cli(self, run_id):
        data = copy.deepcopy(sample())
        data["fixture"] = {"origin": self.origin, "run_id": run_id}
        data["limits"]["wall_seconds"] = 3
        with tempfile.TemporaryDirectory() as tmp:
            task_path = Path(tmp) / "task.json"
            script_path = Path(tmp) / "script.json"
            output_dir = Path(tmp) / "out"
            task_path.write_text(json.dumps(data), encoding="utf-8")
            script_path.write_text('[{"action":"finish"}]', encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-m", "reflexmesh", "run", "--input", str(task_path),
                 "--output-dir", str(output_dir), "--routing-provider", "stub",
                 "--action-provider", "script", "--script", str(script_path)],
                env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                capture_output=True, text=True, timeout=12)
            self.assertEqual(proc.returncode, 3, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(json.loads((output_dir / "result.json").read_text()), result)
            self.assertEqual(result["attempt_status"], "blocked")
            self.assertEqual(result["budget"]["dispatches"], 0)
            self.assertEqual(result["budget"]["model_calls"], 0)
            self.assertEqual(result["actions"], [])
            for name in ("trace.jsonl", "evidence.jsonl", "result.json"):
                self.assertNotIn("secret-text", (output_dir / name).read_text())
            return result

    def test_cli_wrong_run_is_blocked_with_fixture_mismatch(self):
        result = self.run_cli("wrong-fixture-run")
        self.assertEqual(result["stop_reason"], "fixture_mismatch")
        self.assertEqual(read_fixture(self.task(), 1)["phase"], "unclaimed")

    def test_cli_real_busy_is_distinct_and_does_not_disturb_owner(self):
        task = self.task()
        first = FixtureOwnership(mp.get_context("fork"), task, "existing-owner")
        first.acquire(1)
        result = self.run_cli(self.run_id)
        self.assertEqual(result["stop_reason"], "fixture_busy")
        current = read_fixture(task, 1)
        self.assertEqual(current["phase"], "active")
        self.assertEqual(current["binding"], first.binding)

    def test_cli_wrong_run_while_busy_still_reports_fixture_mismatch(self):
        task = self.task()
        first = FixtureOwnership(mp.get_context("fork"), task, "existing-owner")
        first.acquire(1)
        result = self.run_cli("wrong-fixture-run")
        self.assertEqual(result["stop_reason"], "fixture_mismatch")
        current = read_fixture(task, 1)
        self.assertEqual(current["phase"], "active")
        self.assertEqual(current["binding"], first.binding)


if __name__ == "__main__":
    unittest.main()
