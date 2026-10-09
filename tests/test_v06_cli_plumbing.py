"""Task API plumbing on top of main's runtime: chain accounting with optional budgets, attempt
naming, the cancellation handshake, the refused fault flag and result schema 0.2."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.api.service import ApiError, TaskService
from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.contracts.task import ValidationError
from reflexmesh.runtime.cli import validate_chain
from reflexmesh.runtime.handoff import build_handoff, chain_usage, exhausted, remaining_budget
from test_v06_api import ENV, NO_CHROME, free_port, synthetic_result, task_for

ID = "c" * 32
OTHER = "d" * 32


def chain(*, limits=None, used=None, sequence=2):
    return {"root_attempt_id": ID, "parent_attempt_id": ID, "sequence": sequence,
            "root_limits": limits or {"wall_seconds": 30, "max_steps": 10, "max_model_calls": 10,
                                      "max_action_retries": 0},
            "used": used or {"steps": 2, "model_calls": 1, "wall_seconds": 5.0}}


def task(**limits):
    data = task_for(free_port())
    data["limits"].update(limits)
    return ExecutionTask.from_dict(data)


class RemainingBudget(unittest.TestCase):
    def test_finite_limits_subtract_what_the_chain_used(self):
        left = remaining_budget({"wall_seconds": 30, "max_steps": 10, "max_model_calls": 10},
                                {"steps": 4, "model_calls": 1, "wall_seconds": 12.5})
        self.assertEqual(left, {"steps": 6, "model_calls": 9, "wall_seconds": 17.5})
        self.assertFalse(exhausted(left))

    def test_a_disabled_limit_stays_unlimited_and_is_never_exhausted(self):
        left = remaining_budget({"wall_seconds": None, "max_steps": None, "max_model_calls": None},
                                {"steps": 999, "model_calls": 999, "wall_seconds": 9999})
        self.assertEqual(left, {"steps": None, "model_calls": None, "wall_seconds": None})
        self.assertFalse(exhausted(left))

    def test_one_disabled_limit_does_not_hide_an_exhausted_one(self):
        left = remaining_budget({"wall_seconds": None, "max_steps": 3, "max_model_calls": None},
                                {"steps": 3, "model_calls": 0, "wall_seconds": 1})
        self.assertEqual(left["steps"], 0)
        self.assertTrue(exhausted(left))

    def test_less_than_one_unit_left_is_exhausted(self):
        self.assertTrue(exhausted({"steps": 5, "model_calls": 5, "wall_seconds": 0.9}))
        self.assertFalse(exhausted({"steps": 1, "model_calls": 1, "wall_seconds": 1}))


class ChainUsage(unittest.TestCase):
    def test_usage_adds_to_what_the_parent_chain_used(self):
        result = {"budget": {"steps": 3, "model_calls": 2, "local_model_calls": 2, "elapsed_seconds": 4.25}}
        self.assertEqual(chain_usage({"steps": 1, "model_calls": 1, "wall_seconds": 1.0}, result),
                         {"steps": 4, "model_calls": 3, "wall_seconds": 5.25})

    def test_unmetered_router_calls_do_not_break_accounting(self):
        # model_calls is None when router-internal usage is unknown; local requests are counted.
        result = {"budget": {"steps": 1, "model_calls": None, "local_model_calls": 2, "elapsed_seconds": 1.0}}
        self.assertEqual(chain_usage(None, result)["model_calls"], 2)

    def test_missing_budget_counts_as_nothing_used(self):
        self.assertEqual(chain_usage(None, {}), {"steps": 0, "model_calls": 0, "wall_seconds": 0.0})


class ChainValidation(unittest.TestCase):
    def test_a_valid_chain_is_accepted(self):
        raw = chain()
        self.assertIs(validate_chain(raw, task(wall_seconds=10, max_steps=5, max_model_calls=5)), raw)

    def test_a_task_may_not_exceed_what_the_chain_has_left(self):
        for limits in ({"wall_seconds": 26}, {"max_steps": 9}, {"max_model_calls": 10}):
            with self.assertRaises(ValidationError, msg=limits):
                validate_chain(chain(), task(**limits))

    def test_an_unlimited_root_accepts_any_task_limits(self):
        unlimited = {"wall_seconds": None, "max_steps": None, "max_model_calls": None, "max_action_retries": 0}
        validate_chain(chain(limits=unlimited), task(wall_seconds=500, max_steps=500, max_model_calls=500))

    def test_a_limited_root_refuses_a_task_with_a_disabled_limit(self):
        # An unlimited task cannot fit inside a finite remainder.
        data = task_for(free_port())
        data["limits"]["max_steps"] = None
        with self.assertRaises(ValidationError):
            validate_chain(chain(), ExecutionTask.from_dict(data))

    def test_malformed_chains_are_refused(self):
        bad = [{**chain(), "extra": 1}, {**chain(), "sequence": 1}, {**chain(), "sequence": True},
               {**chain(), "root_attempt_id": "XYZ"},
               chain(used={"steps": -1, "model_calls": 0, "wall_seconds": 0}),
               chain(used={"steps": None, "model_calls": 0, "wall_seconds": 0}),
               chain(used={"steps": 0, "model_calls": 0}),
               chain(limits={"wall_seconds": float("inf"), "max_steps": 1, "max_model_calls": 1,
                             "max_action_retries": 0})]
        for raw in bad:
            with self.assertRaises(ValidationError, msg=raw):
                validate_chain(raw, task(wall_seconds=1, max_steps=1, max_model_calls=1))


class HandoffWithOptionalBudgets(unittest.TestCase):
    def test_an_unlimited_root_always_allows_continuation_after_a_clean_stop(self):
        t = task()
        unlimited = {"wall_seconds": None, "max_steps": None, "max_model_calls": None, "max_action_retries": 0}
        result = synthetic_result(t, used={"steps": 500, "model_calls": 500, "wall_seconds": 500})
        handoff = build_handoff(t, result, root_limits=unlimited, usage=result["chain"]["used"])
        self.assertEqual(handoff["continuation"], {"allowed": True, "reason": None})
        self.assertEqual(handoff["remaining_budget"], {"steps": None, "model_calls": None, "wall_seconds": None})


def run_cli(*extra, files=None):
    """Run `reflexmesh run` against a fixture nobody listens on: it ends fast, blocked."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        (directory / "task.json").write_text(json.dumps(task_for(free_port())))
        (directory / "script.json").write_text('[{"action":"finish"}]')
        for name, content in (files or {}).items():
            (directory / name).write_text(json.dumps(content))
        extra = [str(directory / part[1:]) if part.startswith("@") else part for part in extra]
        command = [sys.executable, "-m", "reflexmesh", "run", "--input", str(directory / "task.json"),
                   "--output-dir", str(directory / "out"), "--routing-provider", "stub",
                   "--action-provider", "script", "--script", str(directory / "script.json"),
                   "--chrome", NO_CHROME]
        process = subprocess.run(command + extra, env=ENV, capture_output=True, timeout=60, cwd=directory)
        stdout = process.stdout.decode().strip()
        return process.returncode, (json.loads(stdout) if stdout.startswith("{") else None), process.stderr.decode()


class RunFlags(unittest.TestCase):
    def test_a_standalone_result_is_schema_0_2_with_its_own_chain_and_a_handoff(self):
        code, result, _ = run_cli()
        self.assertEqual(code, 3)
        self.assertEqual(result["schema_version"], "execution-result/0.2")
        self.assertEqual((result["chain"]["root_attempt_id"], result["chain"]["parent_attempt_id"],
                          result["chain"]["sequence"]), (result["attempt_id"], None, 1))
        self.assertEqual(result["handoff"]["kind"], "blocked")
        self.assertEqual(result["chain"]["used"]["model_calls"], 0)

    def test_the_named_attempt_id_is_the_attempts_id(self):
        _, result, _ = run_cli("--attempt-id", OTHER)
        self.assertEqual(result["attempt_id"], OTHER)
        self.assertEqual(result["handoff"]["attempt"]["attempt_id"], OTHER)

    def test_an_invalid_attempt_id_is_refused_before_any_output(self):
        code, result, stderr = run_cli("--attempt-id", "NOT-HEX")
        self.assertEqual(code, 2)
        self.assertIsNone(result)
        self.assertIn("invalid_input", stderr)

    def test_a_chain_link_extends_the_chain_and_accumulates_usage(self):
        link = chain(limits={"wall_seconds": 60, "max_steps": 20, "max_model_calls": 20, "max_action_retries": 0},
                     used={"steps": 2, "model_calls": 1, "wall_seconds": 5.0}, sequence=3)
        code, result, _ = run_cli("--chain", "@chain.json", "--attempt-id", OTHER, files={"chain.json": link})
        self.assertEqual(code, 3)
        self.assertEqual((result["chain"]["root_attempt_id"], result["chain"]["parent_attempt_id"],
                          result["chain"]["sequence"]), (ID, ID, 3))
        self.assertGreaterEqual(result["chain"]["used"]["steps"], 2)
        self.assertGreaterEqual(result["chain"]["used"]["wall_seconds"], 5.0)
        self.assertEqual(result["handoff"]["attempt"]["sequence"], 3)
        self.assertLessEqual(result["handoff"]["remaining_budget"]["steps"], 18)

    def test_a_chain_the_task_does_not_fit_in_is_refused(self):
        link = chain(limits={"wall_seconds": 60, "max_steps": 8, "max_model_calls": 20, "max_action_retries": 0},
                     used={"steps": 7, "model_calls": 0, "wall_seconds": 0.0})
        code, result, stderr = run_cli("--chain", "@chain.json", files={"chain.json": link})
        self.assertEqual(code, 2)
        self.assertIsNone(result)
        self.assertIn("invalid_input", stderr)

    def test_the_cancellation_handshake_announces_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp)
            _, result, _ = run_cli("--control-dir", str(control))
            self.assertTrue((control / "ready").exists())
            self.assertEqual(result["handoff"]["kind"], "blocked")

    def test_a_cancel_requested_before_the_handler_existed_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp)
            (control / "cancel").touch()
            code, result, _ = run_cli("--control-dir", str(control))
            self.assertEqual((result["attempt_status"], result["stop_reason"]), ("cancelled", "cancel_requested"))
            self.assertEqual(code, 130)

    def test_a_fault_file_is_refused_not_silently_ignored(self):
        code, result, stderr = run_cli("--faults", "@faults.json", files={"faults.json": {"hang_close": True}})
        self.assertEqual(code, 2)
        self.assertIsNone(result)
        self.assertIn("faults_unsupported", stderr)

    def test_an_invalid_fault_file_is_an_invalid_input(self):
        code, _, stderr = run_cli("--faults", "@faults.json", files={"faults.json": {"no_such_fault": True}})
        self.assertEqual(code, 2)
        self.assertIn("invalid_input", stderr)


class ServiceContinuationWithoutLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = TaskService(Path(self.tmp.name) / "state", chrome=NO_CHROME, env=ENV)

    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()

    def parent(self, limits, attempt_id="e" * 32):
        data = task_for(free_port(), wall=30)
        data["limits"] = limits
        directory = self.service.root / "attempts" / attempt_id
        (directory / "out").mkdir(parents=True)
        (directory / "task.json").write_text(json.dumps(data))
        (directory / "request.json").write_text(json.dumps({"attempt_id": attempt_id, "submitted_at": 0}))
        parsed = ExecutionTask.from_dict(data)
        result = synthetic_result(parsed, status="failed", reason="postcondition_failed", gaps=[],
                                  used={"steps": 400, "model_calls": 400, "wall_seconds": 4000},
                                  attempt_id=attempt_id)
        root = ({"wall_seconds": None, "max_steps": None, "max_model_calls": None, "max_action_retries": 0}
                if limits is None else limits)
        result["handoff"] = build_handoff(parsed, result, root_limits=root, usage=result["chain"]["used"])
        (directory / "out" / "result.json").write_text(json.dumps(result))
        return attempt_id, data

    def test_an_unlimited_parent_chain_is_continued_unlimited(self):
        parent_id, _ = self.parent(None)
        started = self.service.continue_task(parent_id, {}, "stub", "script", [{"action": "finish"}])
        self.assertEqual(started["limits"], {"wall_seconds": None, "max_steps": None, "max_model_calls": None,
                                             "max_action_retries": 0})
        self.assertEqual(started["chain_sequence"], 2)

    def test_a_limited_parent_with_nothing_left_is_refused(self):
        limits = {"wall_seconds": 30, "max_steps": 8, "max_model_calls": 8, "max_action_retries": 0}
        parent_id, _ = self.parent(limits)
        with self.assertRaises(ApiError) as caught:
            self.service.continue_task(parent_id, {}, "stub", "script", [{"action": "finish"}])
        self.assertEqual(caught.exception.code, "budget_exhausted")


if __name__ == "__main__":
    unittest.main()
