"""Ownership client/evidence unit specifications; no browser or model is required."""

import copy
import io
import json
import multiprocessing as mp
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.ownership import FixtureOwnership, OwnershipError
from reflexmesh.runtime.ownership import _ERROR_BODY_BYTES


PRIVATE_VALUE = "  Do not export this value: 雪\t\r\n"


def task():
    return ExecutionTask.from_dict({
        "schema_version": "execution-task/0.1", "task_id": "owner-task", "revision": 4,
        "goal": "Fill the approved field", "allowed_executors": ["browser.soh"],
        "fixture": {"origin": "http://127.0.0.1:8765", "run_id": "owner-run"},
        "start_path": "/form", "permissions": ["navigate", "type_text"],
        "text_slots": [{"id": "name", "version": 1, "value": PRIVATE_VALUE}],
        "criteria": [{"id": "intact", "kind": "postcondition", "predicate": "account_intact", "args": {}}],
        "limits": None,
    })


def receipt(owner):
    return {"binding": {**owner.identity, "instance_id": "instance-1", "owner_epoch": 1},
            "baseline": {"run_id": owner.identity["run_id"], "instance_id": "instance-1",
                         "sequence": 7, "state": {"private": PRIVATE_VALUE}, "log": []},
            "phase": "active"}


def snapshot(owner, *, bound=False, phase="active", source=None):
    source = receipt(owner) if source is None else copy.deepcopy(source)
    binding = copy.deepcopy(source["binding"])
    if bound:
        binding.update(session_id="session-1", profile_id="profile-1")
    return {"run_id": binding["run_id"], "instance_id": binding["instance_id"],
            "owner_epoch": binding["owner_epoch"], "sequence": 8,
            "state": {"form": {"name": PRIVATE_VALUE}}, "log": [], "binding": binding,
            "phase": phase, "effects": [], "pending_effects": [], "baseline": source["baseline"]}


class Response(io.BytesIO):
    status = 200


class OwnershipClient(unittest.TestCase):
    def owner(self):
        return FixtureOwnership(mp.get_context("fork"), task(), "attempt-1")

    def acquired(self):
        owner = self.owner()
        with patch.object(owner, "_request", return_value=receipt(owner)):
            owner.acquire(0.2)
        return owner

    def bound(self):
        owner = self.acquired()
        with patch.object(owner, "_request", return_value=snapshot(owner, bound=True)):
            owner.bind_session("session-1", "profile-1", timeout=0.2)
        return owner

    def test_identity_and_secrets_are_private_per_attempt_and_copied(self):
        first, second = self.owner(), self.owner()
        self.assertEqual(first.identity_tuple, ("owner-task", 4, "attempt-1", "owner-run"))
        self.assertNotEqual(first.identity["claim_id"], second.identity["claim_id"])
        self.assertNotEqual(first.browser_secret, second.browser_secret)
        self.assertNotEqual(first.browser_secret, first._owner_secret)
        self.assertGreaterEqual(len(first.browser_secret), 32)
        first.identity["claim_id"] = "forged"
        self.assertNotEqual(first.identity["claim_id"], "forged")
        for secret in (first._owner_secret, first.browser_secret, PRIVATE_VALUE):
            self.assertNotIn(secret, repr(first))
            self.assertNotIn(secret, json.dumps(first.export()))

    def test_claim_attempt_flag_precedes_network_and_claim_is_single_shot(self):
        owner = self.owner()

        def open_request(request, *, timeout):
            self.assertEqual(owner.acquisition_attempted.value, 1)
            self.assertEqual(request.full_url, task().origin + "/__claim")
            self.assertEqual(request.method, "POST")
            body = json.loads(request.data)
            self.assertEqual(body["identity"], owner.identity)
            self.assertEqual(set(body), {"identity", "owner_secret", "browser_secret"})
            self.assertEqual(timeout, 0.2)
            return Response(json.dumps(receipt(owner)).encode())

        opener = Mock()
        opener.open.side_effect = open_request
        with patch("reflexmesh.runtime.ownership.urllib.request.build_opener", return_value=opener):
            owner.acquire(0.2)
            with self.assertRaisesRegex(OwnershipError, "ownership_acquisition_already_attempted"):
                owner.acquire(0.2)
        self.assertEqual(opener.open.call_count, 1)
        self.assertTrue(owner.is_claimed)
        self.assertTrue(owner.is_active)
        self.assertEqual(owner.baseline["sequence"], 7)

    def test_lost_acquire_response_recovers_baseline_with_owner_identity(self):
        owner = self.owner()
        opener = Mock()
        opener.open.side_effect = TimeoutError("secret response body")
        with patch("reflexmesh.runtime.ownership.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(OwnershipError, "ownership_unavailable"):
                owner.acquire(0.2)
        self.assertEqual(owner.acquisition_attempted.value, 1)
        self.assertIsNone(owner.binding)
        self.assertFalse(owner.export())
        with patch.object(owner, "_request", return_value=snapshot(owner)) as read:
            owner.read(0.2)
        self.assertEqual(read.call_args.args[1], {"owner_secret": owner._owner_secret, "identity": owner.identity})
        self.assertEqual(owner.baseline["sequence"], 7)
        self.assertEqual(owner.view().status, "unknown")

    def test_worker_receipt_is_only_a_hint_until_authenticated_read(self):
        owner = self.owner()
        owner.adopt(receipt(owner))
        self.assertTrue(owner.is_claimed)
        self.assertFalse(owner.is_active)
        self.assertIsNone(owner.verified_binding)
        self.assertIsNone(owner.baseline)
        self.assertFalse(owner.export())
        self.assertEqual(owner.view().evidence_refs, ())
        with patch.object(owner, "_request", return_value=snapshot(owner, bound=True)) as read:
            owner.read(0.2)
        self.assertIn("identity", read.call_args.args[1])
        self.assertEqual(owner.binding["session_id"], "session-1")
        self.assertEqual(owner.verified_binding, owner.binding)
        self.assertTrue(owner.export())
        self.assertEqual(owner.view().status, "unknown")
        self.assertIsNone(owner.view().observed_at)
        self.assertEqual(owner.view().evidence_refs, ())

    def test_foreign_identity_wrong_types_and_incomplete_binding_are_unknown(self):
        changes = ({"task_id": "foreign"}, {"task_revision": True}, {"task_revision": 5},
                   {"attempt_id": "foreign"}, {"run_id": "foreign"}, {"claim_id": "foreign"},
                   {"owner_epoch": True}, {"owner_epoch": 0}, {"instance_id": ""},
                   {"session_id": "session-only"}, {"profile_id": "/private/profile/path"})
        for change in changes:
            with self.subTest(change=change):
                owner = self.owner()
                value = receipt(owner)
                value["binding"].update(change)
                with patch.object(owner, "_request", return_value=value):
                    with self.assertRaises(OwnershipError):
                        owner.acquire(0.2)
                self.assertEqual(owner.view().status, "unknown")
                self.assertFalse(owner.export())

    def test_instance_restart_and_foreign_response_do_not_prove_violation(self):
        for change in ({"instance_id": "restarted"}, {"owner_epoch": 2}, {"claim_id": "foreign"}):
            with self.subTest(change=change):
                owner = self.bound()
                value = snapshot(owner, bound=True)
                value["binding"].update(change)
                with patch.object(owner, "_request", return_value=value):
                    with self.assertRaises(OwnershipError):
                        owner.read(0.2)
                self.assertEqual(owner.view().status, "unknown")
                self.assertFalse(any(row["status"] == "fail" for row in owner.export()))

    def test_bind_and_assert_are_exact_local_checks_and_never_an_ownership_pass(self):
        owner = self.bound()
        self.assertIsNone(owner.assert_session("session-1", "profile-1"))
        with self.assertRaisesRegex(OwnershipError, "ownership_session_mismatch"):
            owner.assert_session("session-2", "profile-1")
        self.assertEqual(owner.view().status, "unknown")
        # A caller's mismatching local argument alone is not a proven violation.
        with patch.object(owner, "_request") as network:
            with self.assertRaises(OwnershipError):
                owner.bind_session("session-2", "profile-1")
            network.assert_not_called()
        owner.binding["session_id"] = "mutated-copy"
        self.assertEqual(owner.binding["session_id"], "session-1")

    def test_lost_bind_response_uses_recovery_read_without_retrying_bind(self):
        owner = self.acquired()
        with patch.object(owner, "_request", side_effect=OwnershipError("ownership_unavailable")) as network:
            with self.assertRaises(OwnershipError):
                owner.bind_session("session-1", "profile-1")
            with self.assertRaisesRegex(OwnershipError, "ownership_operation_already_attempted"):
                owner.bind_session("session-1", "profile-1")
            self.assertEqual(network.call_count, 1)
        self.assertFalse(owner.is_active)
        with patch.object(owner, "_request", return_value=snapshot(owner, bound=True)) as network:
            owner.read(0.2)
        self.assertIn("identity", network.call_args.args[1])
        owner.assert_session("session-1", "profile-1")

    def test_authoritative_own_binding_session_change_is_a_sticky_source_backed_failure(self):
        owner = self.bound()
        value = snapshot(owner, bound=True)
        value["binding"]["session_id"] = "changed-session"
        with patch.object(owner, "_request", return_value=value):
            with self.assertRaisesRegex(OwnershipError, "ownership_session_conflict"):
                owner.read(0.2)
        failed = owner.view()
        self.assertEqual(failed.status, "fail")
        exported = {row["ref"]: row for row in owner.export()}
        for reference in failed.evidence_refs:
            row = exported[reference]
            self.assertEqual(row["source"], "authenticated_fixture_response")
            self.assertEqual(row["previous_response_binding"]["session_id"], "session-1")
            self.assertEqual(row["response_binding"]["session_id"], "changed-session")
        with patch.object(owner, "_request", side_effect=OwnershipError("ownership_unavailable")):
            with self.assertRaises(OwnershipError):
                owner.read(0.2)
        self.assertEqual(owner.view().status, "fail")

    def test_release_requires_confirmed_cleanup_and_server_quarantine(self):
        owner = self.bound()
        with patch.object(owner, "_request") as network:
            for cleanup in ("unknown", "failed", None, True, ""):
                with self.subTest(cleanup=cleanup), self.assertRaisesRegex(OwnershipError, "ownership_cleanup_unconfirmed"):
                    owner.release(cleanup)
            with self.assertRaisesRegex(OwnershipError, "ownership_not_quarantined"):
                owner.release("closed")
            network.assert_not_called()
        with patch.object(owner, "_request", return_value=snapshot(owner, bound=True, phase="quarantined")):
            owner.revoke(0.2)
        self.assertFalse(owner.is_active)
        with patch.object(owner, "_request", return_value=snapshot(owner, bound=True, phase="released")) as network:
            owner.release("forced", 0.2)
        payload = network.call_args.args[1]
        self.assertEqual(set(payload), {"owner_secret", "binding", "cleanup_confirmed"})
        self.assertIs(payload["cleanup_confirmed"], True)
        self.assertEqual(payload["binding"], owner.binding)
        self.assertEqual(owner.view().status, "unknown")

    def test_uncertain_revoke_or_release_is_never_repeated(self):
        owner = self.bound()
        with patch.object(owner, "_request", side_effect=OwnershipError("ownership_unavailable")) as network:
            with self.assertRaises(OwnershipError):
                owner.revoke(0.2)
            with self.assertRaisesRegex(OwnershipError, "ownership_operation_already_attempted"):
                owner.revoke(0.2)
            self.assertEqual(network.call_count, 1)
        with patch.object(owner, "_request", return_value=snapshot(owner, bound=True, phase="quarantined")):
            owner.read(0.2)
        with patch.object(owner, "_request", side_effect=OwnershipError("ownership_unavailable")) as network:
            with self.assertRaises(OwnershipError):
                owner.release("closed", 0.2)
            with self.assertRaisesRegex(OwnershipError, "ownership_operation_already_attempted"):
                owner.release("closed", 0.2)
            self.assertEqual(network.call_count, 1)

    def test_preclaim_fence_is_authoritative_metadata_without_fabricated_claim(self):
        owner = self.owner()
        value = {"phase": "revoked_before_claim", "identity": owner.identity, "instance_id": "instance-1"}
        with patch.object(owner, "_request", return_value=value) as network:
            self.assertEqual(owner.revoke(0.2), value)
        self.assertIn("identity", network.call_args.args[1])
        self.assertIsNone(owner.binding)
        self.assertIsNone(owner.baseline)
        self.assertIsNone(owner.verified_binding)
        self.assertFalse(owner.is_active)
        self.assertEqual(owner.view().status, "unknown")
        self.assertEqual(owner.export()[-1]["kind"], "fixture_preclaim_fence")
        self.assertNotIn("owner_epoch", owner.export()[-1])
        with patch.object(owner, "_request") as network:
            with self.assertRaisesRegex(OwnershipError, "ownership_not_quarantined"):
                owner.release("closed", 0.2)
            network.assert_not_called()
        with patch.object(owner, "_request", return_value=value):
            self.assertEqual(owner.read(0.2), value)

    def test_foreign_preclaim_fence_is_unknown_not_a_confirmed_violation(self):
        owner = self.owner()
        value = {"phase": "revoked_before_claim", "identity": {**owner.identity, "claim_id": "foreign"},
                 "instance_id": "instance-1"}
        with patch.object(owner, "_request", return_value=value):
            with self.assertRaises(OwnershipError):
                owner.revoke(0.2)
        self.assertFalse(owner.export())
        self.assertEqual(owner.view().status, "unknown")

    def test_shared_boundary_witness_survives_lost_worker_receipts(self):
        parent = self.owner()
        # Model fork-local ordinary attributes with genuinely shared raw arrays.
        worker = copy.copy(parent)
        worker._public = []
        worker._attempted_operations = set()
        with patch.object(worker, "_request", return_value=receipt(worker)):
            worker.acquire(0.2)
        with patch.object(worker, "_request", return_value=snapshot(worker, bound=True)):
            worker.bind_session("session-1", "profile-1")
        self.assertTrue(worker._record_browser_violation(
            "browser_session_changed", session_id="actual-session-2"))
        self.assertIsNone(parent.verified_binding)
        view = parent.view()
        self.assertEqual(view.status, "fail")
        exported = {row["ref"]: row for row in parent.export()}
        self.assertEqual(len(view.evidence_refs), 1)
        row = exported[view.evidence_refs[0]]
        self.assertEqual(row["source"], "attempt_memory")
        self.assertEqual(row["binding"]["session_id"], "session-1")
        self.assertEqual(row["observed_session_id"], "actual-session-2")
        self.assertNotIn(worker._owner_secret, json.dumps(exported))
        self.assertEqual(parent.view().status, "fail")
        self.assertEqual(len(parent.export()), 1)

    def test_missing_boundary_identity_and_unsupported_reasons_cannot_write_failure(self):
        owner = self.owner()
        self.assertFalse(owner._record_browser_violation("browser_session_changed", session_id="new"))
        owner.adopt(receipt(owner))
        self.assertFalse(owner._record_browser_violation("browser_profile_changed"))
        self.assertEqual(owner.view().status, "unknown")
        owner = self.bound()
        self.assertFalse(owner._record_browser_violation("missing_session"))
        self.assertFalse(owner._record_browser_violation("caller_says_failure"))
        self.assertEqual(owner.view().status, "unknown")

    def test_boundary_profile_comparison_never_exports_private_paths(self):
        owner = self.bound()
        path = "/private/temporary/profile/with-sensitive-data"
        self.assertTrue(owner._record_browser_violation("browser_profile_changed", profile_id=path))
        self.assertEqual(owner.view().status, "fail")
        self.assertNotIn(path, json.dumps(owner.export()))
        self.assertIsNone(owner.export()[-1]["observed_profile_id"])

    def test_bearer_material_cannot_be_exported_as_public_identity(self):
        owner = self.acquired()
        with patch.object(owner, "_request") as network:
            with self.assertRaises(OwnershipError):
                owner.bind_session(owner.browser_secret, "profile-1")
            network.assert_not_called()
        owner = self.bound()
        self.assertTrue(owner._record_browser_violation(
            "browser_session_changed", session_id=owner.browser_secret,
            profile_id="prefix-" + owner._owner_secret))
        self.assertEqual(owner.view().status, "fail")
        encoded = json.dumps(owner.export())
        self.assertNotIn(owner.browser_secret, encoded)
        self.assertNotIn(owner._owner_secret, encoded)
        self.assertIsNone(owner.export()[-1]["observed_session_id"])
        self.assertIsNone(owner.export()[-1]["observed_profile_id"])

    def test_export_has_only_redacted_authoritative_metadata_and_source_refs(self):
        owner = self.bound()
        value = snapshot(owner, bound=True)
        event = {"seq": 8, "t": 123.0, "method": "POST", "path": "/form", "binding": owner.binding}
        value["log"] = [event, {"data": PRIVATE_VALUE}]
        value["effects"] = [event, {**event, "data": PRIVATE_VALUE}]
        value["pending_effects"] = [{"path": "/slow/export", "binding": owner.binding}]
        with patch.object(owner, "_request", return_value=value):
            owner.read(0.2)
        rows = owner.export()
        encoded = json.dumps(rows, ensure_ascii=False)
        for secret in (PRIVATE_VALUE, owner._owner_secret, owner.browser_secret):
            self.assertNotIn(secret, encoded)
        self.assertNotIn('"state"', encoded)
        self.assertNotIn('"data"', encoded)
        self.assertNotIn('"log"', encoded)
        self.assertNotIn('"pass"', encoded)
        row = rows[-1]
        self.assertEqual(len(row["effects"]), 1)
        self.assertEqual(row["atomic_baseline_seq"], 7)
        self.assertEqual(row["source_ref"],
                         f"fixture:owner-run:instance-1:{owner.identity['claim_id']}:1:8")
        row["response_binding"]["claim_id"] = "mutated-copy"
        self.assertNotEqual(owner.export()[-1]["response_binding"]["claim_id"], "mutated-copy")

    def test_http_errors_and_malformed_json_are_sanitized_and_not_retried(self):
        for status, expected in ((409, "ownership_protocol_error"), (403, "ownership_authentication_failed"),
                                 (404, "ownership_unsupported"), (500, "ownership_protocol_error"),
                                 (307, "ownership_protocol_error")):
            with self.subTest(status=status):
                owner = self.owner()
                opener = Mock()
                opener.open.side_effect = urllib.error.HTTPError(
                    task().origin + "/__claim", status, PRIVATE_VALUE, {}, io.BytesIO(PRIVATE_VALUE.encode()))
                with patch("reflexmesh.runtime.ownership.urllib.request.build_opener", return_value=opener):
                    with self.assertRaisesRegex(OwnershipError, expected) as error:
                        owner.acquire(0.2)
                self.assertNotIn(PRIVATE_VALUE, str(error.exception))
                self.assertEqual(opener.open.call_count, 1)
                self.assertFalse(owner.export())
        owner = self.owner()
        opener = Mock()
        opener.open.return_value = Response(b'{"phase":"active","phase":"released"}')
        with patch("reflexmesh.runtime.ownership.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(OwnershipError, "ownership_response_invalid"):
                owner.acquire(0.2)

    def test_claim_conflict_codes_are_exact_allowlisted_server_errors(self):
        for code, expected in (("fixture_mismatch", "fixture_mismatch"),
                               ("fixture_owned", "fixture_busy"),
                               ("claim_revoked", "ownership_conflict")):
            with self.subTest(code=code):
                owner = self.owner()
                body = io.BytesIO(json.dumps({"error": code}).encode())
                opener = Mock()
                opener.open.side_effect = urllib.error.HTTPError(
                    task().origin + "/__claim", 409, PRIVATE_VALUE, {}, body)
                with patch("reflexmesh.runtime.ownership.urllib.request.build_opener", return_value=opener):
                    with self.assertRaises(OwnershipError) as error:
                        owner.acquire(0.2)
                    self.assertEqual(error.exception.reason, expected)
                    with self.assertRaisesRegex(OwnershipError, "ownership_acquisition_already_attempted"):
                        owner.acquire(0.2)
                self.assertEqual(opener.open.call_count, 1)
                self.assertTrue(body.closed)
                self.assertEqual(owner.acquisition_attempted.value, 1)
                self.assertIsNone(owner.binding)
                self.assertFalse(owner.export())

    def test_claim_error_envelope_rejects_malformed_oversized_and_private_values(self):
        for case in ("arbitrary", "private_value", "owner_secret", "browser_secret", "extra_field",
                     "list", "null", "boolean", "duplicate", "truncated", "invalid_utf8",
                     "oversized", "oversized_valid_prefix"):
            with self.subTest(case=case):
                owner = self.owner()
                cases = {
                    "arbitrary": b'{"error":"caller_selected_stop_reason"}',
                    "private_value": json.dumps({"error": PRIVATE_VALUE}).encode(),
                    "owner_secret": json.dumps({"error": owner._owner_secret}).encode(),
                    "browser_secret": json.dumps({"error": owner.browser_secret}).encode(),
                    "extra_field": json.dumps({"error": "fixture_mismatch", "secret": owner.browser_secret}).encode(),
                    "list": b'["fixture_mismatch"]', "null": b'{"error":null}',
                    "boolean": b'{"error":true}',
                    "duplicate": b'{"error":"fixture_owned","error":"fixture_mismatch"}',
                    "truncated": b'{"error":"fixture_mismatch"', "invalid_utf8": b'\xff',
                    "oversized": b' ' * (_ERROR_BODY_BYTES + 1) + b'{"error":"fixture_mismatch"}',
                    "oversized_valid_prefix": b'{"error":"fixture_mismatch"}' + b' ' * _ERROR_BODY_BYTES,
                }
                body = io.BytesIO(cases[case])
                opener = Mock()
                opener.open.side_effect = urllib.error.HTTPError(
                    task().origin + "/__claim", 409, PRIVATE_VALUE, {}, body)
                with patch("reflexmesh.runtime.ownership.urllib.request.build_opener", return_value=opener):
                    with self.assertRaises(OwnershipError) as error:
                        owner.acquire(0.2)
                self.assertEqual(error.exception.reason, "ownership_protocol_error")
                self.assertEqual(opener.open.call_count, 1)
                self.assertTrue(body.closed)
                self.assertFalse(owner.export())
                public = str(error.exception) + repr(error.exception) + owner.view().reason + json.dumps(owner.export())
                for private in (PRIVATE_VALUE, owner._owner_secret, owner.browser_secret):
                    self.assertNotIn(private, public)

    def test_claim_error_body_read_is_single_bounded_and_read_failure_is_sanitized(self):
        owner = self.owner()
        stream = Mock()
        stream.read.return_value = b'{"error":"fixture_mismatch"}'
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(task().origin + "/__claim", 409, "Conflict", {}, stream)
        with patch("reflexmesh.runtime.ownership.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(OwnershipError, "fixture_mismatch"):
                owner.acquire(0.2)
        stream.read.assert_called_once_with(_ERROR_BODY_BYTES + 1)
        for error in (OSError(PRIVATE_VALUE), ValueError(PRIVATE_VALUE)):
            with self.subTest(error_type=type(error)):
                owner = self.owner()
                stream = Mock()
                stream.read.side_effect = error
                opener = Mock()
                opener.open.side_effect = urllib.error.HTTPError(
                    task().origin + "/__claim", 409, "Conflict", {}, stream)
                with patch("reflexmesh.runtime.ownership.urllib.request.build_opener", return_value=opener):
                    with self.assertRaisesRegex(OwnershipError, "ownership_protocol_error") as caught:
                        owner.acquire(0.2)
                self.assertNotIn(PRIVATE_VALUE, str(caught.exception))
                self.assertEqual(opener.open.call_count, 1)
                stream.read.assert_called_once_with(_ERROR_BODY_BYTES + 1)

    def test_invalid_timeout_makes_no_network_request(self):
        for timeout in (0, -1, True, float("inf"), float("nan"), 10 ** 10000):
            with self.subTest(timeout_type=type(timeout)):
                owner = self.owner()
                with patch("reflexmesh.runtime.ownership.urllib.request.build_opener") as opener:
                    with self.assertRaisesRegex(OwnershipError, "ownership_timeout"):
                        owner.acquire(timeout)
                    opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
