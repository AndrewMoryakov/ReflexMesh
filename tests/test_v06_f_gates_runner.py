"""The F-gate runner (experiments/v06/f_checks/run_gates.py) must not report a pass it did not observe.

It runs the ledger's tests in child processes. These tests replace the child with a stub that ends in a chosen way,
so no browser is needed: a stale result file, a crashed child and a bad pass count each must fail the report.
"""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("run_gates", ROOT / "experiments/v06/f_checks/run_gates.py")
GATES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATES)

LEDGER = json.loads((ROOT / "experiments/v06/f_checks/ledger.json").read_text("utf-8"))
F11_TESTS = sorted({t for item in LEDGER["F11"]["items"] for t in item["tests"]})
ALL_OK = "import json, sys; json.dump({n: 'ok' for n in sys.argv[2:]}, open(sys.argv[1], 'w'))"


class RunnerReport(unittest.TestCase):
    def run_runner(self, child, *extra, before=None):
        """Run the runner on gate F11 with `child` as the test process; returns (exit code, report or None, out dir)."""
        self.assertTrue(F11_TESTS)
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        if before:
            before(directory)
        original, GATES.CHILD = GATES.CHILD, child
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = GATES.main(["--out", str(directory), "--only", "F11", *extra])
        finally:
            GATES.CHILD = original
        report = directory / "report.json"
        return code, (json.loads(report.read_text("utf-8")) if report.exists() else None), directory

    @staticmethod
    def f11(report):
        return {row["need"]: row["status"] for row in report["gates"]["F11"]}

    def test_a_child_that_crashes_does_not_inherit_the_results_of_an_earlier_run(self):
        def stale(directory):                 # the earlier run's results, all ok, in the reused output directory
            (directory / "pass1.json").write_text(json.dumps({t: "ok" for t in F11_TESTS}), "utf-8")

        code, report, directory = self.run_runner("import sys; sys.exit(3)", "--passes", "1", before=stale)
        self.assertEqual(code, 2)
        self.assertEqual(set(self.f11(report).values()), {"fail"})
        self.assertEqual(report["meta"]["child_returncodes"], [3])
        self.assertFalse((directory / "pass1.json").exists())

    def test_a_child_that_ends_cleanly_without_writing_results_is_a_failed_pass(self):
        code, report, _ = self.run_runner("pass", "--passes", "1")
        self.assertEqual(code, 2)
        self.assertEqual(set(self.f11(report).values()), {"fail"})
        self.assertEqual({o for v in report["tests"].values() for o in v}, {"missing"})

    def test_a_child_that_writes_results_but_exits_with_an_error_is_a_failed_pass(self):
        code, report, _ = self.run_runner(ALL_OK + "; sys.exit(1)", "--passes", "1")
        self.assertEqual(code, 2)
        self.assertEqual(set(self.f11(report).values()), {"fail"})

    def test_a_pass_count_below_one_is_refused_before_anything_runs(self):
        for count in ("0", "-2"):
            with self.subTest(count=count):
                directory = Path(tempfile.mkdtemp())
                self.addCleanup(lambda d=directory: __import__("shutil").rmtree(d, ignore_errors=True))
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                    GATES.main(["--out", str(directory), "--only", "F11", "--passes", count])
                self.assertEqual(stopped.exception.code, 2)
                self.assertFalse((directory / "report.json").exists())

    def test_fewer_than_three_passes_cannot_report_a_pass(self):
        for passes in ("1", "2"):
            with self.subTest(passes=passes):
                code, report, _ = self.run_runner(ALL_OK, "--passes", passes)
                self.assertEqual(code, 0)
                self.assertEqual(set(self.f11(report).values()), {"diagnostic"})

    def test_three_clean_passes_report_a_pass(self):
        code, report, _ = self.run_runner(ALL_OK, "--passes", "3")
        self.assertEqual(code, 0)
        self.assertEqual(set(self.f11(report).values()), {"pass"})

    def test_one_failure_in_one_pass_fails_the_item(self):
        child = ("import json, sys; json.dump({n: ('FAIL' if sys.argv[1].endswith('pass2.json') else 'ok') "
                 "for n in sys.argv[2:]}, open(sys.argv[1], 'w'))")
        code, report, _ = self.run_runner(child, "--passes", "3")
        self.assertEqual(code, 0)
        self.assertEqual(set(self.f11(report).values()), {"fail"})


if __name__ == "__main__":
    unittest.main()
