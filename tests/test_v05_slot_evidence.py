"""Private evidence ledger regressions; no browser or model is executed here.

These tests exercise trusted producer primitives. Real adapter-boundary and
supervisor paths are covered separately; ledger tests are not B acceptance.
"""

import hashlib
import json
import multiprocessing as mp
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.text.slots import SlotRegistry
from reflexmesh.tracing.slot_evidence import SlotEvidence
from reflexmesh.verification.constraints import ConstraintAssessments, assess_constraints


VALUE = "  Private\tvalue\r\n雪 e\u0301  "


def request(value=VALUE):
    return ExecutionTask.from_dict({
        "schema_version": "execution-task/0.1", "task_id": "slot-task", "revision": 7,
        "goal": "Fill the approved field", "allowed_executors": ["browser.soh"],
        "fixture": {"origin": "http://127.0.0.1:8765", "run_id": "slot-run"},
        "start_path": "/form", "permissions": ["navigate", "type_text"],
        "text_slots": [{"id": "name", "version": 3, "value": value}],
        "criteria": [{"id": "intact", "kind": "postcondition", "predicate": "account_intact", "args": {}}],
        "limits": None,
    })


def coverage(*kinds, sealed=True):
    count = len(kinds)
    return {"dispatches": count, "text_dispatches": kinds.count("text"),
            "nontext_dispatches": kinds.count("nontext"), "unknown_dispatches": kinds.count("unknown"),
            "generation": count, "sealed_generation": count if sealed else -1}


class BrokenSender:
    def send(self, packet):
        raise OSError("private evidence transport unavailable")

    def close(self):
        pass


class _EvidenceFixture(unittest.TestCase):
    def setUp(self):
        self.context = mp.get_context("fork")
        self.ledgers = []

    def tearDown(self):
        for ledger in self.ledgers:
            ledger.close()

    def ledger(self, task=None, attempt="attempt"):
        task = request() if task is None else task
        ledger = SlotEvidence(self.context, task, attempt, registry=SlotRegistry.from_task(task))
        ledger.bind_dispatch_counter(self.context.Value("q", 1, lock=False))
        self.ledgers.append(ledger)
        return ledger

    def matched(self, task=None, attempt="attempt"):
        task = request() if task is None else task
        ledger = self.ledger(task, attempt)
        self.assertTrue(ledger.sink.commit(1, "text", "name@3"))
        self.assertTrue(ledger.sink.handoff(1, "name@3", task.slots[0].value))
        return ledger


class EvidenceLedger(_EvidenceFixture):
    def test_exact_input_passes_and_refs_resolve_to_retained_records(self):
        ledger = self.matched()
        view = ledger.view(coverage("text"))
        self.assertEqual(view.status, "pass")
        exported = {row["ref"]: row for row in ledger.export()}
        self.assertTrue(view.evidence_refs)
        self.assertTrue(all(reference in exported for reference in view.evidence_refs))
        self.assertEqual({row["kind"] for row in exported.values()},
                         {"commitment", "handoff", "sealed_coverage"})
        self.assertTrue(all(row["capture_storage"] == "attempt_memory" for row in exported.values()))

    def test_empty_whitespace_unicode_and_surrogates_are_exact(self):
        for value in ("", " \t\r\n ", "雪🚀", "e\u0301", "\ud800"):
            with self.subTest(value=repr(value)):
                ledger = self.matched(request(value))
                self.assertEqual(ledger.view(coverage("text")).status, "pass")
        for expected, actual in ((" a ", "a"), ("e\u0301", "é"), ("a\r\nb", "a\nb"), ("", " ")):
            with self.subTest(expected=repr(expected), actual=repr(actual)):
                ledger = self.ledger(request(expected))
                ledger.sink.commit(1, "text", "name@3")
                self.assertFalse(ledger.sink.handoff(1, "name@3", actual))
                self.assertEqual(ledger.view(None).status, "fail")

    def test_supported_nontext_coverage_can_pass_but_unknown_cannot(self):
        for kind, expected in (("nontext", "pass"), ("unknown", "unknown")):
            with self.subTest(kind=kind):
                ledger = self.ledger()
                ledger.sink.commit(1, kind)
                self.assertEqual(ledger.view(coverage(kind)).status, expected)
        ledger = self.ledger()
        self.assertEqual(ledger.view(coverage()).status, "unknown")

    def test_missing_handoff_and_missing_commitment_are_unknown(self):
        ledger = self.ledger()
        ledger.sink.commit(1, "text", "name@3")
        self.assertEqual(ledger.view(coverage("text")).status, "unknown")
        ledger = self.ledger()
        ledger._dispatch_counter.value = 2
        ledger.sink.commit(2, "nontext")
        self.assertEqual(ledger.view(coverage("nontext", "nontext")).status, "unknown")
        self.assertEqual(ledger.view(coverage("nontext")).status, "unknown")

    def test_unsealed_or_inconsistent_generation_cannot_pass(self):
        ledger = self.matched()
        self.assertEqual(ledger.view(coverage("text", sealed=False)).status, "unknown")
        for changes in ({"sealed_generation": 0}, {"generation": 2}, {"dispatches": 2},
                        {"text_dispatches": 0}, {"unknown_dispatches": 1},
                        {"generation": True}, {"sealed_generation": True}):
            with self.subTest(changes=changes):
                self.assertEqual(ledger.view({**coverage("text"), **changes}).status, "unknown")
        self.assertEqual(ledger.view(coverage("text")).status, "pass")

    def test_duplicate_commitment_or_receipt_is_unknown(self):
        for duplicate in ("commitment", "handoff"):
            with self.subTest(duplicate=duplicate):
                ledger = self.matched()
                if duplicate == "commitment":
                    ledger.sink.commit(1, "text", "name@3")
                else:
                    ledger.sink.handoff(1, "name@3", VALUE)
                self.assertEqual(ledger.view(coverage("text")).status, "unknown")

    def test_every_foreign_identity_and_generation_is_rejected(self):
        for field, value in (("task_id", "foreign-task"), ("task_revision", 8),
                             ("attempt_id", "foreign-attempt"), ("run_id", "foreign-run"),
                             ("action_id", 2), ("generation", 2), ("slot_ref", "name@4")):
            with self.subTest(field=field):
                ledger = self.ledger()
                record = ledger.sink._base("commitment", 1)
                record.update(classification="text", slot_ref="name@3")
                record[field] = value
                ledger.sink._send(record)
                self.assertEqual(ledger.view(coverage("text")).status, "unknown")

    def test_foreign_receipt_action_slot_or_boundary_is_not_a_mismatch_witness(self):
        for action, reference, boundary in ((2, "name@3", "systemone.browser_use.tools.act.input/v1"),
                                            (1, "name@4", "systemone.browser_use.tools.act.input/v1"),
                                            (1, "name@3", "unverified-adapter")):
            with self.subTest(action=action, reference=reference, boundary=boundary):
                ledger = self.ledger()
                ledger.sink.commit(1, "text", "name@3")
                self.assertFalse(ledger.sink.handoff(action, reference, "wrong", boundary=boundary))
                self.assertEqual(ledger._failed.value, 0)
                self.assertEqual(ledger.view(coverage("text")).status, "unknown")

    def test_signed_foreign_commitment_and_wrong_handoff_cannot_establish_failure(self):
        ledger = self.ledger()
        record = ledger.sink._base("commitment", 999)
        record.update(classification="text", slot_ref="name@3")
        self.assertTrue(ledger.sink._send(record))
        record = ledger.sink._base("handoff", 999)
        record.update(classification="text", slot_ref="name@3",
                      boundary="systemone.browser_use.tools.act.input/v1",
                      expected=ledger._expected["name@3"], actual="0" * 64)
        self.assertTrue(ledger.sink._send(record))
        self.assertEqual(ledger.view(coverage("text")).status, "unknown")
        self.assertEqual(ledger._failed.value, 0)
        self.assertFalse(any(row.get("status") == "fail" for row in ledger.export()))

    def test_unbound_ledger_and_rebinding_are_rejected(self):
        ledger = SlotEvidence(self.context, request(), "unbound")
        self.ledgers.append(ledger)
        self.assertFalse(ledger.sink.commit(1, "text", "name@3"))
        self.assertFalse(ledger.sink.handoff(1, "name@3", "wrong"))
        self.assertEqual(ledger.view(coverage("text")).status, "unknown")
        self.assertEqual(ledger._failed.value, 0)
        bound = self.ledger()
        with self.assertRaises(ValueError):
            bound.bind_dispatch_counter(self.context.Value("q", 2, lock=False))

    def test_forged_unsigned_or_cross_attempt_packet_never_passes(self):
        for packet in (b'{"status":"pass","slots_preserved":true}',
                       b'{"record":{},"authentication":"invented"}', b"[" * 1200 + b"]" * 1200):
            with self.subTest(packet=packet[:50]):
                ledger = self.matched()
                ledger.sink._sender.send(packet)
                self.assertEqual(ledger.view(coverage("text")).status, "unknown")
        ledger, foreign = self.matched(), self.ledger(attempt="another-attempt")
        foreign.sink.commit(1, "text", "name@3")
        packet = foreign._receiver.recv(16384)
        ledger.sink._sender.send(packet)
        self.assertEqual(ledger.view(coverage("text")).status, "unknown")

    def test_loss_downgrades_match_but_shared_mismatch_survives_receipt_loss(self):
        for actual, expected in ((VALUE, "unknown"), ("wrong", "fail")):
            with self.subTest(expected=expected):
                ledger = self.ledger()
                ledger.sink.commit(1, "text", "name@3")
                ledger.drain()
                original = ledger.sink._sender
                ledger.sink._sender = BrokenSender()
                try:
                    ledger.sink.handoff(1, "name@3", actual)
                    view = ledger.view(None)
                    self.assertEqual(view.status, expected)
                    if expected == "fail":
                        witnesses = {row["ref"]: row for row in ledger.export()}
                        self.assertTrue(all(reference in witnesses for reference in view.evidence_refs))
                        witness = witnesses[view.evidence_refs[0]]
                        self.assertEqual((witness["kind"], witness["source"], witness["action_id"],
                                          witness["slot_ref"]),
                                         ("mismatch_witness", "shared_boundary_observation", 1, "name@3"))
                finally:
                    ledger.sink._sender = original

    def test_mismatch_survives_lost_commitment_and_all_later_evidence_loss(self):
        ledger = self.ledger()
        original = ledger.sink._sender
        ledger.sink._sender = BrokenSender()
        try:
            self.assertFalse(ledger.sink.commit(1, "text", "name@3"))
            ledger.sink.handoff(1, "name@3", "wrong")
        finally:
            ledger.sink._sender = original
        self.assertEqual(ledger.view(None).status, "fail")
        ledger._invalid = True
        ledger.close()
        self.assertEqual(ledger.view(None).status, "fail")

    def test_draining_a_prefix_is_not_complete_and_export_is_a_pure_copy(self):
        ledger = self.matched()
        ledger.sink.handoff(1, "name@3", VALUE)  # Duplicate must be observed before any pass.
        with patch("reflexmesh.tracing.slot_evidence._DRAIN_BATCH", 2):
            self.assertEqual(ledger.view(coverage("text")).status, "unknown")
        before = ledger.export()
        self.assertEqual(len(before), 2)
        before[0]["kind"] = "forged"
        self.assertEqual(ledger.export()[0]["kind"], "commitment")
        self.assertEqual(ledger.view(coverage("text")).status, "unknown")

    def test_no_total_action_cap_with_disabled_limits(self):
        ledger = self.ledger()
        count = 2048
        for action_id in range(1, count + 1):
            ledger._dispatch_counter.value = action_id
            self.assertTrue(ledger.sink.commit(action_id, "nontext"))
            ledger.drain()
        self.assertEqual(ledger.view(coverage(*(["nontext"] * count))).status, "pass")
        self.assertEqual(len(ledger._commitments), count)

    def test_public_evidence_contains_no_plaintext_unkeyed_digest_or_private_key(self):
        ledger = self.matched()
        ledger.view(coverage("text"))
        exported = json.dumps(ledger.export(), ensure_ascii=False)
        self.assertNotIn(VALUE, exported)
        self.assertNotIn(hashlib.sha256(VALUE.encode()).hexdigest(), exported)
        self.assertNotIn(ledger._key.hex(), exported)
        self.assertNotIn(ledger._expected["name@3"], exported)
        for row in ledger.export():
            self.assertFalse({"actual", "expected", "authentication", "key", "value", "text"} & set(row))


class IndependentAssessment(_EvidenceFixture):
    def test_budget_snapshot_loss_does_not_erase_slot_pass_or_fail(self):
        for actual, expected in ((VALUE, "pass"), ("wrong", "fail")):
            with self.subTest(expected=expected):
                task = request()
                ledger = self.ledger(task)
                ledger.sink.commit(1, "text", "name@3")
                ledger.sink.handoff(1, "name@3", actual)
                rows = assess_constraints(task, None, "attempt", ledger, coverage("text"))
                self.assertEqual(rows[1]["status"], expected)
                self.assertEqual([row["status"] for row in rows if row["id"] != "runtime.immutable_slots"],
                                 ["unknown"] * 5)

    def test_four_other_contracts_stay_unknown_when_slots_and_budget_pass(self):
        task = request()
        ledger = self.matched(task)
        rows = assess_constraints(task, {"steps": 100000, "model_calls": 100000}, "attempt",
                                  ledger, coverage("text"))
        self.assertEqual([row["status"] for row in rows],
                         ["unknown", "pass", "unknown", "unknown", "unknown", "pass"])

    def test_worker_or_verifier_dict_assertions_and_foreign_ledger_are_ignored(self):
        task = request()
        for claimed in ({"status": "pass", "slots_preserved": True},
                        {"id": "runtime.immutable_slots", "status": "fail", "evidence_refs": ["fake"]},
                        self.matched(task, attempt="foreign-attempt")):
            with self.subTest(claimed_type=type(claimed).__name__):
                rows = assess_constraints(task, None, "attempt", claimed, coverage("text"))
                self.assertEqual(rows[1]["status"], "unknown")

    def test_real_failure_is_sticky_against_later_missing_or_passing_assessments(self):
        task = request()
        failed = self.ledger(task)
        failed.sink.commit(1, "text", "name@3")
        failed.sink.handoff(1, "name@3", "wrong")
        assessments = ConstraintAssessments()
        assessments.update(assess_constraints(task, None, "attempt", failed, None))
        passed = self.matched(task)
        for rows in (assess_constraints(task, None, "attempt"),
                     assess_constraints(task, None, "attempt", passed, coverage("text"))):
            assessments.update(rows, retain_passes=True)
            self.assertEqual(assessments.rows()[1]["status"], "fail")
            self.assertTrue(assessments.has_fail())


if __name__ == "__main__":
    unittest.main()
