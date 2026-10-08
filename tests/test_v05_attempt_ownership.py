"""Parent/fixture ownership integration with real loopback HTTP and owned workers.

HTTP clients deliberately simulate the browser cookie and session identity.
These sources exercise claim attribution and lifecycle, not real-browser
acceptance, and never fabricate passing runtime constraints.
"""

import copy
import io
import json
import multiprocessing as mp
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.cli import BrowserExecutionStrategy, run_execution
from reflexmesh.runtime.ownership import FixtureOwnership, OwnershipError
from reflexmesh.runtime.runner import AttemptSupervisor, RuntimeStop, WorkerResult
from reflexmesh.verification.verifier import FixtureVerifier, read_fixture
from test_v05_fixture import FixtureServerCase
from test_v05_optional_limits import finite_watchdog
from test_v05_runtime import sample


LINUX_OWNED_WORKER = (sys.platform.startswith("linux") and hasattr(os, "WNOWAIT") and
                      hasattr(signal, "setitimer") and "fork" in mp.get_all_start_methods())


def browser_request(task, owner, path, data=None):
    request = urllib.request.Request(task.origin + path, data=data,
                                     headers={"Cookie": "ReflexMeshOwner=" + owner.browser_secret})
    with urllib.request.urlopen(request, timeout=2) as response:
        response.read()


def claim_in_worker(task, gate, events, *, publish=True):
    owner = gate.fixture_owner
    receipt = owner.acquire(1)
    if publish:
        events.put(("fixture_claim", receipt))
    owner.bind_session("http-worker-session", "http-worker-profile", timeout=1)
    return owner


def owned_http_dispatch(task, gate, events, owner, operation, path, data=None):
    # An explicitly synthetic HTTP worker reports to the production gate. It
    # cannot acquire the trusted browser adapter's immutable-slot producer.
    gate.reserve_step()
    events.put(("proposal", {"action": operation}))
    action_id = gate.commit_dispatch()
    events.put(("dispatch", action_id, {"operation": operation, "ownership": owner.binding}))
    try:
        browser_request(task, owner, path, data)
        events.put(("driver_return", action_id, True))
    finally:
        gate.settle(action_id)


def submit_once(task, gate, events, *, publish=True):
    owner = claim_in_worker(task, gate, events, publish=publish)
    values = urllib.parse.urlencode({"name": "secret-text", "email": "owner@example.test"}).encode()
    owned_http_dispatch(task, gate, events, owner, "submit_form", "/form", values)
    return WorkerResult("finish")


def ownership_row(result):
    return next(row for row in result["verification"] if row["id"] == "runtime.ownership")


@unittest.skipUnless(LINUX_OWNED_WORKER, "ownership lifecycle requires Linux owned workers")
class AttemptOwnershipLifecycle(FixtureServerCase):
    slow_seconds = 0.6

    def task(self, predicate="account_intact", args=None):
        task = super().task(predicate, args)
        return replace(task, limits=replace(task.limits, wall_seconds=3))

    def form_task(self):
        data = copy.deepcopy(sample())
        data["fixture"] = {"origin": self.origin, "run_id": self.run_id}
        data["limits"]["wall_seconds"] = 3
        data["text_slots"].append({"id": "email", "version": 1, "value": "owner@example.test"})
        data["criteria"] = [{"id": "sent", "kind": "postcondition", "predicate": "form_submitted_once",
                             "args": {"name_slot": "name@1", "email_slot": "email@1"}}]
        return ExecutionTask.from_dict(data)

    def run_bounded(self, supervisor):
        try:
            with finite_watchdog(8), patch("reflexmesh.runtime.runner.GRACE_SECONDS", 0.3):
                result = supervisor.run()
            self.assertFalse(supervisor.process.is_alive(), "owned worker leaked")
            json.dumps(result, allow_nan=False)
            return result
        finally:
            if supervisor.process.pid is not None and not supervisor.process._released:
                supervisor.process.cleanup(1)
            supervisor.events.close()
            supervisor.slot_evidence.close()

    def test_real_preflight_claim_is_parent_owned_and_released_without_browser(self):
        task = self.task()
        args = SimpleNamespace(routing_provider="stub", jev_url="http://127.0.0.1:8787",
                               timeout=2, chrome=None)
        supervisor = AttemptSupervisor(task, BrowserExecutionStrategy(task, args, None),
                                       verifier_factory=FixtureVerifier)
        self.assertIs(supervisor.fixture_owner, supervisor.gate.fixture_owner)
        self.assertEqual(supervisor.fixture_owner.identity["attempt_id"], supervisor.attempt_id)
        with patch("reflexmesh.runtime.cli.importlib.util.find_spec", return_value=None):
            result = self.run_bounded(supervisor)
        current = read_fixture(task, 1)
        self.assertEqual(result["stop_reason"], "executor_unavailable")
        self.assertEqual(result["cleanup"], "closed")
        self.assertEqual(current["phase"], "released")
        self.assertEqual(current["binding"]["attempt_id"], result["attempt_id"])
        self.assertEqual(result["budget"]["dispatches"], 0)
        self.assertEqual(ownership_row(result)["status"], "unknown")
        serialized = json.dumps(result)
        self.assertNotIn(supervisor.fixture_owner.browser_secret, serialized)
        self.assertNotIn("secret-text", serialized)
        self.assertNotIn("fixture_claim", [row["kind"] for row in result["trace"]])

    def test_accepted_cancel_prevents_later_claim_marker_and_http(self):
        for wall in (3, None):
            with self.subTest(wall=wall):
                task = self.task()
                task = replace(task, limits=replace(task.limits, wall_seconds=wall))
                supervisor = AttemptSupervisor(task, lambda *_: WorkerResult("finish"))
                try:
                    self.assertTrue(supervisor.cancel())
                    with patch.object(supervisor.fixture_owner, "_request") as request:
                        with self.assertRaises(RuntimeStop) as stopped:
                            supervisor.fixture_owner.acquire(1)
                    self.assertEqual(stopped.exception.reason, "cancel_requested")
                    self.assertEqual(supervisor.fixture_owner.acquisition_attempted.value, 0)
                    request.assert_not_called()
                    self.assertEqual(read_fixture(task, 1)["phase"], "unclaimed")
                finally:
                    supervisor.events.close()
                    supervisor.slot_evidence.close()

    def test_owned_submission_is_attributed_without_claiming_full_completion(self):
        task = self.form_task()
        supervisor = AttemptSupervisor(task, lambda gate, events: submit_once(task, gate, events),
                                       verifier_factory=FixtureVerifier)
        result = self.run_bounded(supervisor)
        self.assertEqual(result["verification"][0]["status"], "pass")
        self.assertEqual(result["verification"][0]["verified_operations"], ["submit_form"])
        self.assertEqual(result["actions"][0]["effect"], "applied")
        self.assertEqual(result["actions"][0]["ownership"]["attempt_id"], supervisor.attempt_id)
        self.assertEqual(result["attempt_status"], "incomplete")
        self.assertEqual(result["stop_reason"], "verification_unknown")
        self.assertEqual(ownership_row(result)["status"], "unknown")
        self.assertEqual(read_fixture(task, 1)["phase"], "released")
        self.assertNotIn("secret-text", json.dumps(result))

    def test_lost_claim_receipt_stays_unknown_but_parent_recovers_and_releases(self):
        task = self.form_task()
        supervisor = AttemptSupervisor(
            task, lambda gate, events: submit_once(task, gate, events, publish=False),
            verifier_factory=FixtureVerifier)
        result = self.run_bounded(supervisor)
        self.assertEqual(result["verification"][0]["status"], "unknown")
        self.assertEqual(result["actions"][0]["effect"], "unknown")
        self.assertEqual(result["attempt_status"], "incomplete")
        self.assertEqual(read_fixture(task, 1)["phase"], "released")

    def test_cli_retains_redacted_authenticated_ownership_records(self):
        data = copy.deepcopy(sample())
        data["fixture"] = {"origin": self.origin, "run_id": self.run_id}
        data["limits"]["wall_seconds"] = 3
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            args = SimpleNamespace(input="task.json", output_dir=str(output), no_limits=False,
                                   routing_provider="stub", action_provider="jev", script=None,
                                   chrome=None, jev_url="http://127.0.0.1:8787", timeout=2)
            stdout = io.StringIO()
            with (finite_watchdog(8), patch("reflexmesh.runtime.cli._load", return_value=data),
                  patch("reflexmesh.runtime.cli.importlib.util.find_spec", return_value=None),
                  patch("reflexmesh.runtime.cli.signal.signal"), redirect_stdout(stdout)):
                self.assertEqual(run_execution(args), 3)
            result_text = (output / "result.json").read_text(encoding="utf-8")
            evidence_text = (output / "evidence.jsonl").read_text(encoding="utf-8")
            trace_text = (output / "trace.jsonl").read_text(encoding="utf-8")
            result = json.loads(result_text)
            self.assertEqual(json.loads(stdout.getvalue()), result)
            records = [json.loads(line) for line in evidence_text.splitlines()]
            self.assertTrue(result["ownership_evidence"])
            for record in result["ownership_evidence"]:
                self.assertIn(record, records)
                self.assertEqual(record["attempt_id"], result["attempt_id"])
                self.assertEqual(record["source"], "authenticated_fixture_response")
            refs = {record["ref"] for record in records}
            self.assertTrue(set(result["evidence_refs"]) <= refs)
            serialized = "\n".join((result_text, evidence_text, trace_text, stdout.getvalue()))
            for private in ("secret-text", "owner_secret", "browser_secret", "ReflexMeshOwner="):
                self.assertNotIn(private, serialized)

    def test_foreign_submission_cannot_satisfy_unclaimed_bootstrap(self):
        task = self.form_task()
        public_baseline = read_fixture(task, 1)
        foreign_owner, _ = self.claim(task)
        values = urllib.parse.urlencode({"name": "secret-text", "email": "owner@example.test"}).encode()
        self.request(foreign_owner, "/form", values).close()

        def bootstrap_only(gate, events):
            events.put(("baseline", public_baseline))
            gate.reserve_step()
            action_id = gate.commit_dispatch()
            events.put(("dispatch", action_id, {"operation": "navigate"}))
            events.put(("driver_return", action_id, True))
            events.put(("observation", {"url": task.origin + "/form", "run_id": task.run_id,
                                        "target_ids": ["form"]}))
            gate.settle(action_id)
            return WorkerResult("finish")

        result = self.run_bounded(AttemptSupervisor(task, bootstrap_only, verifier_factory=FixtureVerifier))
        self.assertEqual(result["verification"][0]["status"], "unknown")
        self.assertEqual(result["attempt_status"], "incomplete")
        self.assertEqual(result["budget"]["dispatches"], 1)
        self.assertEqual(read_fixture(task, 1)["binding"]["attempt_id"], foreign_owner.identity["attempt_id"])

    def test_prior_owner_form_state_does_not_satisfy_next_claim_bootstrap(self):
        task = self.form_task()
        previous, _ = self.claim(task)
        values = urllib.parse.urlencode({"name": "secret-text", "email": "owner@example.test"}).encode()
        self.request(previous, "/form", values).close()
        previous.revoke(1)
        previous.release("closed", 1)

        def only_get(gate, events):
            owner = claim_in_worker(task, gate, events)
            owned_http_dispatch(task, gate, events, owner, "navigate", "/form")
            events.put(("observation", {"url": task.origin + "/form", "run_id": task.run_id,
                                        "target_ids": ["form"], "ownership": owner.binding}))
            return WorkerResult("finish")

        result = self.run_bounded(AttemptSupervisor(task, only_get, verifier_factory=FixtureVerifier))
        self.assertEqual(result["verification"][0]["status"], "fail")
        self.assertEqual(result["stop_reason"], "postcondition_failed")
        self.assertNotEqual(result["attempt_id"], previous.identity["attempt_id"])

    def test_cancel_fences_pending_export_before_process_cleanup(self):
        task = self.task("export_completed_once")
        pending = mp.get_context("fork").Event()

        def export_worker(gate, events):
            owner = claim_in_worker(task, gate, events)

            def delayed_request():
                try:
                    browser_request(task, owner, "/slow/export", b"")
                except OSError:
                    pass  # Revocation or process cleanup must interrupt it.

            thread = threading.Thread(target=delayed_request, daemon=True)
            thread.start()
            until = time.monotonic() + 1
            while time.monotonic() < until:
                if owner.read(1)["pending_effects"]:
                    pending.set()
                    break
                time.sleep(0.005)
            threading.Event().wait()

        supervisor = AttemptSupervisor(task, export_worker, verifier_factory=FixtureVerifier)
        cancelled = []

        def cancel():
            ready = pending.wait(2)
            cancelled.append((ready, supervisor.cancel()))

        cancellation = threading.Thread(target=cancel, daemon=True)
        cancellation.start()
        cleanup_phases = []
        original_cleanup = supervisor.process.cleanup

        def check_cleanup(allowance):
            cleanup_phases.append(read_fixture(task, 1)["phase"])
            return original_cleanup(allowance)

        try:
            with patch.object(supervisor.process, "cleanup", side_effect=check_cleanup):
                result = self.run_bounded(supervisor)
        finally:
            cancellation.join(2)
        self.assertFalse(cancellation.is_alive())
        self.assertEqual(cancelled, [(True, True)])
        self.assertTrue(cleanup_phases)
        self.assertEqual(cleanup_phases[0], "quarantined")
        self.assertEqual((result["attempt_status"], result["stop_reason"]),
                         ("cancelled", "cancel_requested"))
        time.sleep(self.slow_seconds)
        self.assertEqual(read_fixture(task, 1)["state"]["exports"], 0)

    def test_unknown_cleanup_retains_quarantine_and_blocks_another_attempt(self):
        task = self.task()

        def owned_finish(gate, events):
            claim_in_worker(task, gate, events)
            return WorkerResult("finish")

        supervisor = AttemptSupervisor(task, owned_finish, verifier_factory=FixtureVerifier)
        original_cleanup = supervisor.process.cleanup

        def uncertain_cleanup(allowance):
            original_cleanup(allowance)  # Actually reap; fault only the evidence.
            return "unknown"

        with patch.object(supervisor.process, "cleanup", side_effect=uncertain_cleanup):
            result = self.run_bounded(supervisor)
        self.assertEqual(result["cleanup"], "unknown")
        self.assertEqual(read_fixture(task, 1)["phase"], "quarantined")
        contender = FixtureOwnership(mp.get_context("fork"), task, "next-attempt")
        with self.assertRaises(OwnershipError) as blocked:
            contender.acquire(1)
        self.assertEqual(blocked.exception.reason, "fixture_busy")

    def test_late_real_owned_pass_cannot_refine_accepted_cancel(self):
        task = self.form_task()
        context = mp.get_context("fork")
        entered, returned, proved = context.Event(), context.Event(), context.Event()
        supervisor = None

        def late_verifier(task, timeout, observation):
            owner = supervisor.fixture_owner
            current = owner.read(1)
            rows = FixtureVerifier(task, current["baseline"], ownership=owner)(task, timeout, observation)
            if rows[0]["status"] == "pass":
                proved.set()
            stopping = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stopping.set())
            entered.set()
            stopping.wait(2)
            returned.set()
            return rows

        supervisor = AttemptSupervisor(task, lambda gate, events: submit_once(task, gate, events),
                                       verifier=late_verifier)
        cancelled = []

        def cancel():
            ready = entered.wait(2)
            cancelled.append((ready, supervisor.cancel()))

        cancellation = threading.Thread(target=cancel, daemon=True)
        cancellation.start()
        try:
            result = self.run_bounded(supervisor)
        finally:
            cancellation.join(2)
        self.assertEqual(cancelled, [(True, True)])
        self.assertTrue(proved.is_set(), "verifier never obtained the real owned pass")
        self.assertTrue(returned.is_set())
        self.assertEqual(result["attempt_status"], "cancelled")
        self.assertEqual(result["verification"][0]["status"], "unknown")
        self.assertEqual(result["actions"][0]["effect"], "unknown")
        self.assertEqual(result["actions"][0]["evidence_refs"], [])

    def test_mutually_matching_forged_bindings_cannot_reconcile_parent_actions(self):
        task = self.form_task()
        supervisor = AttemptSupervisor(task, lambda *_: WorkerResult("finish"))
        owner = supervisor.fixture_owner
        try:
            owner.acquire(1)
            owner.bind_session("http-parent-session", "http-parent-profile", timeout=1)
            self.assertTrue(supervisor._same_owner(owner.binding, owner.binding))
            changes = ({"instance_id": "different-instance"}, {"owner_epoch": 2},
                       {"attempt_id": "different-attempt"}, {"claim_id": "different-claim"},
                       {"task_revision": True}, {"task_revision": 1.0},
                       {"owner_epoch": True}, {"owner_epoch": 1.0})
            for change in changes:
                with self.subTest(change=change):
                    forged = {**owner.binding, **change}
                    self.assertFalse(supervisor._same_owner(forged, forged))
                    # Two mutually agreeing worker/verifier dictionaries still
                    # cannot replace the parent's authenticated binding.
                    actions = [{"id": 1, "operation": "submit_form", "ownership": forged,
                                "effect": "unknown", "evidence_refs": []}]
                    assessments = [{"id": "sent", "status": "pass", "ownership": forged,
                                    "verified_operations": ["submit_form"],
                                    "evidence_refs": ["test-only:untrusted-claim"]}]
                    self.assertEqual(supervisor._reconcile(actions, assessments), set())
                    self.assertEqual(actions[0]["effect"], "unknown")
                    navigation = []
                    supervisor._record(("dispatch", 1, {"operation": "navigate", "ownership": forged}),
                                       navigation)
                    supervisor._record(("driver_return", 1, True), navigation)
                    supervisor._record(("observation", {"run_id": task.run_id,
                                                        "url": task.origin + task.start_path,
                                                        "target_ids": ["form"], "ownership": forged}),
                                       navigation)
                    self.assertEqual(navigation[0]["effect"], "unknown")
                    self.assertEqual(navigation[0]["evidence_refs"], [])
        finally:
            owner.revoke(1)
            owner.release("closed", 1)
            supervisor.events.close()
            supervisor.slot_evidence.close()

    def test_navigation_observation_must_follow_its_own_driver_return(self):
        task = self.task()
        supervisor = AttemptSupervisor(task, lambda *_: WorkerResult("finish"))
        owner = supervisor.fixture_owner
        try:
            owner.acquire(1)
            owner.bind_session("http-navigation-session", "http-navigation-profile", timeout=1)
            observation = {"run_id": task.run_id, "url": task.origin + task.start_path,
                           "target_ids": ["form"], "ownership": owner.binding}
            for observe_before_dispatch in (True, False):
                with self.subTest(observe_before_dispatch=observe_before_dispatch):
                    actions = []
                    if observe_before_dispatch:
                        supervisor._record(("observation", observation), actions)
                    supervisor._record(("dispatch", 1, {"operation": "navigate",
                                                         "ownership": owner.binding}), actions)
                    if not observe_before_dispatch:
                        supervisor._record(("observation", observation), actions)
                    supervisor._record(("driver_return", 1, True), actions)
                    supervisor._reconcile_navigation(actions)
                    self.assertEqual(actions[0]["effect"], "unknown")
                    self.assertEqual(actions[0]["evidence_refs"], [])
                    supervisor._record(("observation", observation), actions)
                    self.assertEqual(actions[0]["effect"], "applied")
        finally:
            owner.revoke(1)
            owner.release("closed", 1)
            supervisor.events.close()
            supervisor.slot_evidence.close()


class OwnershipVerifierBinding(FixtureServerCase):
    def test_public_baseline_and_missing_session_cannot_pass(self):
        task = self.task()
        public = read_fixture(task, 1)
        self.assertEqual(FixtureVerifier(task, public)(task, 1)[0]["status"], "unknown")
        owner = FixtureOwnership(mp.get_context("fork"), task, "no-browser-session")
        receipt = owner.acquire(1)
        result = FixtureVerifier(task, receipt["baseline"], ownership=owner)(task, 1)
        self.assertEqual(result[0]["status"], "unknown")
        self.assertEqual(result[0]["evidence_refs"], [])

    def test_transport_baseline_must_match_atomic_server_baseline(self):
        task = self.task()
        owner, baseline = self.claim(task)
        for change in ({"sequence": -1}, {"instance_id": "different-instance"},
                       {"state": {"account_deleted": True}}):
            with self.subTest(change=change):
                result = FixtureVerifier(task, {**baseline, **change}, ownership=owner)(task, 1)
                self.assertEqual(result[0]["status"], "unknown")

    def test_foreign_or_missing_observation_binding_cannot_verify_page(self):
        task = self.task("current_page", {"path": "/reports", "target_id": "reports"})
        owner, baseline = self.claim(task)
        self.request(owner, "/reports").close()
        verifier = FixtureVerifier(task, baseline, ownership=owner)
        observation = {"run_id": task.run_id, "url": task.origin + "/reports", "target_ids": ["reports"],
                       "ownership": owner.binding}
        self.assertEqual(verifier(task, 1, observation)[0]["status"], "pass")
        for key in ("attempt_id", "instance_id", "claim_id", "session_id", "profile_id"):
            with self.subTest(key=key):
                foreign = {**observation, "ownership": {**owner.binding, key: "foreign-value"}}
                self.assertEqual(verifier(task, 1, foreign)[0]["status"], "unknown")
        for key in ("task_revision", "owner_epoch"):
            for value in (True, 1.0):
                with self.subTest(key=key, value=value):
                    foreign = {**observation, "ownership": {**owner.binding, key: value}}
                    self.assertEqual(verifier(task, 1, foreign)[0]["status"], "unknown")
        observation.pop("ownership")
        self.assertEqual(verifier(task, 1, observation)[0]["status"], "unknown")

    def test_mixed_foreign_effect_binding_does_not_pass_intact_account(self):
        task = self.task()
        owner, baseline = self.claim(task)
        self.request(owner, "/form", b"name=value&email=owner%40example.test").close()
        current = owner.read(1)
        verifier = FixtureVerifier(task, baseline, ownership=owner)
        self.assertEqual(verifier(task, 1)[0]["status"], "pass")
        for key in ("attempt_id", "instance_id", "claim_id", "owner_epoch", "session_id", "profile_id"):
            with self.subTest(key=key):
                corrupted = copy.deepcopy(current)
                corrupted["effects"][0]["binding"][key] = (999 if key == "owner_epoch" else "foreign-value")
                # Deliberate malformed transport fault after a real acquisition;
                # verifier must reject mixed attribution rather than filter it.
                with patch.object(owner, "read", return_value=corrupted):
                    self.assertEqual(verifier(task, 1)[0]["status"], "unknown")
        for key in ("task_revision", "owner_epoch"):
            for value in (True, 1.0):
                with self.subTest(key=key, value=value):
                    corrupted = copy.deepcopy(current)
                    corrupted["effects"][0]["binding"][key] = value
                    with patch.object(owner, "read", return_value=corrupted):
                        self.assertEqual(verifier(task, 1)[0]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
