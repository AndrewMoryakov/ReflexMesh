"""Private, attempt-bound immutable-slot evidence from the actual driver boundary.

The runtime capability below is given only to the supported adapter. Worker event
messages and verifier assertions never enter this ledger. This is an in-process
trust boundary, not a sandbox against arbitrary Python replacing runtime code.
Private datagrams contain keyed digests, never text; public exports contain only
identities, classifications, and observed results. No key or digest is exported.

A nonblocking datagram socket avoids Queue feeder/partial-frame locks if a worker
is killed. Transport loss prevents a pass. A monotonic shared-memory witness is
written *before* sending a proven mismatch, so losing a receipt cannot erase an
observed failure. Evidence references identify records retained in this attempt;
filesystem persistence is a separate responsibility and is never asserted here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import socket
import time
from dataclasses import dataclass

from reflexmesh.contracts.execution import ExecutionTask

_CLASSIFICATIONS = frozenset({"text", "nontext", "unknown"})
_BOUNDARY = "systemone.browser_use.tools.act.input/v1"
_PACKET_BYTES = 16384
_DRAIN_BATCH = 256


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("ascii")


def _value_digest(key: bytes, slot_ref: str, value: str) -> str:
    # Preserve every Python task string exactly, including lone surrogates. This
    # is not normalization, stripping, interpolation, or a public text hash.
    prefix = _encoded(["immutable-slot-value/1", slot_ref])
    return hmac.new(key, prefix + b"\0" + value.encode("utf-8", "surrogatepass"),
                    hashlib.sha256).hexdigest()


def _signature(key: bytes, record: dict) -> str:
    return hmac.new(key, b"immutable-slot-record/1\0" + _encoded(record),
                    hashlib.sha256).hexdigest()


def _within_committed_actions(counter, action_id: int) -> bool:
    # This lock-free high-water read can only reject a foreign action. A pass
    # still requires the gate's synchronized, sealed coverage snapshot.
    return (counter is not None and type(action_id) is int and
            type(counter.value) is int and 1 <= action_id <= counter.value)


@dataclass(frozen=True)
class _SlotEvidenceView:
    identity: tuple
    status: str
    reason: str
    observed_at: float | None
    evidence_refs: tuple[str, ...]


class _RuntimeSlotSink:
    """Private trusted-boundary capability; no method accepts an expected value."""

    def __init__(self, sender, identity, key, expected, failed, failure_action,
                 failure_slot, failure_time, unavailable):
        self._sender, self._identity, self._key = sender, identity, key
        self._expected = dict(expected)
        self._slot_indexes = {ref: index + 1 for index, ref in enumerate(expected)}
        self._failed, self._failure_action = failed, failure_action
        self._failure_slot, self._failure_time = failure_slot, failure_time
        self._unavailable = unavailable
        self._dispatch_counter = None
        self._committed: dict[int, tuple[str, str | None]] = {}

    def _base(self, kind: str, action_id: int) -> dict:
        task_id, revision, attempt_id, run_id = self._identity
        return {"version": 1, "task_id": task_id, "task_revision": revision,
                "attempt_id": attempt_id, "run_id": run_id, "kind": kind,
                "action_id": action_id, "generation": action_id}

    def _send(self, record: dict) -> bool:
        try:
            packet = _encoded({"record": record, "authentication": _signature(self._key, record)})
            if len(packet) > _PACKET_BYTES:
                self._unavailable.value = 1
                return False
            if self._sender.send(packet) != len(packet):
                self._unavailable.value = 1
                return False
            return True
        except (OSError, ValueError, TypeError, OverflowError, MemoryError, RecursionError):
            self._unavailable.value = 1
            return False

    def commit(self, action_id: int, classification: str,
               slot_ref: str | None = None) -> bool:
        """Record a gate-committed action, outside the gate lock.

        Classification comes from runtime certification, never an adapter's
        self-reported capability or public descriptor. Unsupported adapters are
        classified unknown. An invalid reference cannot prove a text match.
        """
        if (not _within_committed_actions(self._dispatch_counter, action_id) or
                type(classification) is not str or classification not in _CLASSIFICATIONS or
                (slot_ref is not None and type(slot_ref) is not str)):
            self._unavailable.value = 1
            return False
        if ((classification == "text" and slot_ref not in self._expected) or
                (classification != "text" and slot_ref is not None)):
            self._unavailable.value = 1
        if action_id in self._committed:
            self._unavailable.value = 1
        else:
            self._committed[action_id] = (classification, slot_ref)
        record = self._base("commitment", action_id)
        record.update(classification=classification, slot_ref=slot_ref)
        return self._send(record)

    def handoff(self, action_id: int, slot_ref: str, actual_text: str, *,
                boundary: str = _BOUNDARY) -> bool:
        """Compare the actual input handed to the pinned driver, not a proposal.

        The certified producer invokes this just after passing the very same
        input to the captured driver and before awaiting its result. This proves
        call arguments, not coroutine execution or a DOM effect. Only a committed
        text action with the same accepted slot can write a failure witness.
        Unknown identities are missing evidence instead.
        """
        if (not _within_committed_actions(self._dispatch_counter, action_id) or type(slot_ref) is not str or
                type(actual_text) is not str or boundary != _BOUNDARY or
                self._committed.get(action_id) != ("text", slot_ref) or
                slot_ref not in self._expected):
            self._unavailable.value = 1
            return False
        actual = _value_digest(self._key, slot_ref, actual_text)
        expected = self._expected[slot_ref]
        matched = hmac.compare_digest(expected, actual)
        if not matched and not self._failed.value:
            # The worker is the sole witness writer. Publish the flag last. These
            # raw shared scalars need no lock that a terminated worker can hold.
            self._failure_action.value = action_id
            self._failure_slot.value = self._slot_indexes[slot_ref]
            self._failure_time.value = time.time()
            self._failed.value = 1
        record = self._base("handoff", action_id)
        record.update(classification="text", slot_ref=slot_ref, boundary=boundary,
                      expected=expected, actual=actual)
        self._send(record)
        return matched

    def close(self) -> None:
        """Close this process's sending endpoint; no flush/join can block."""
        self._sender.close()


class SlotEvidence:
    """Supervisor-owned authenticated ledger, independent of budget evidence."""

    def __init__(self, ctx, task: ExecutionTask, attempt_id: str, *, registry=None):
        self._identity = (task.task_id, task.revision, attempt_id, task.run_id)
        self._key = secrets.token_bytes(32)
        # Read the accepted task/registry once. Later mutation of an adapter's
        # resolver or its copied task cannot change the expected ledger values.
        values = {slot.reference: slot.value for slot in task.slots}
        if registry is not None:
            references = tuple(registry.references)
            if ((registry.task_id, registry.revision, registry.run_id) !=
                    (task.task_id, task.revision, task.run_id) or set(references) != set(values)):
                raise ValueError("accepted slot registry differs from task")
            for reference, value in values.items():
                if registry.resolve(reference)._value != value:
                    raise ValueError("accepted slot registry differs from task")
        self._expected = {ref: _value_digest(self._key, ref, value)
                          for ref, value in values.items()}
        self._slot_refs = tuple(values)
        self._failed = ctx.Value("i", 0, lock=False)
        self._failure_action = ctx.Value("q", 0, lock=False)
        self._failure_slot = ctx.Value("q", 0, lock=False)
        self._failure_time = ctx.Value("d", 0.0, lock=False)
        self._unavailable = ctx.Value("i", 0, lock=False)
        self._receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._receiver.setblocking(False)
        sender.setblocking(False)
        self.sink = _RuntimeSlotSink(sender, self._identity, self._key, self._expected,
                                     self._failed, self._failure_action, self._failure_slot,
                                     self._failure_time, self._unavailable)
        self._commitments: dict[int, dict] = {}
        self._handoffs: dict[int, dict] = {}
        self._public: dict[str, dict] = {}
        self._invalid = False
        self._closed = False
        self._drained_to_empty = False
        self._dispatch_counter = None

    def bind_dispatch_counter(self, counter) -> None:
        """Bind once to the gate's shared committed-dispatch high-water scalar."""
        if (self._dispatch_counter is not None or counter is None or
                type(getattr(counter, "value", None)) is not int or counter.value < 0):
            raise ValueError("invalid or already bound dispatch counter")
        self._dispatch_counter = counter
        self.sink._dispatch_counter = counter

    def _reference(self, suffix: str) -> str:
        return f"runtime:{self._identity[2]}:immutable-slots:{suffix}"

    def _public_record(self, reference: str, **fields) -> dict:
        task_id, revision, attempt_id, run_id = self._identity
        return {"ref": reference, "task_id": task_id, "task_revision": revision,
                "attempt_id": attempt_id, "run_id": run_id,
                "capture_storage": "attempt_memory", **fields}

    def _failure_witness(self) -> str | None:
        if (not self._failed.value or
                not _within_committed_actions(self._dispatch_counter, self._failure_action.value)):
            return None
        reference = self._reference("mismatch-witness")
        if reference not in self._public:
            index = self._failure_slot.value
            if not 0 < index <= len(self._slot_refs):
                return None
            slot_ref = self._slot_refs[index - 1]
            self._public[reference] = self._public_record(
                reference, kind="mismatch_witness", action_id=self._failure_action.value,
                slot_ref=slot_ref, status="fail", observed_at=self._failure_time.value,
                source="shared_boundary_observation")
        return reference

    def _accept(self, packet: bytes) -> dict | None:
        try:
            envelope = json.loads(packet)
            if type(envelope) is not dict or set(envelope) != {"record", "authentication"}:
                raise ValueError("invalid envelope")
            record, authentication = envelope["record"], envelope["authentication"]
            if (type(record) is not dict or type(authentication) is not str or
                    not hmac.compare_digest(authentication, _signature(self._key, record))):
                raise ValueError("unauthenticated evidence")
            common = {"version", "task_id", "task_revision", "attempt_id", "run_id",
                      "kind", "action_id", "generation", "classification", "slot_ref"}
            kind = record.get("kind")
            fields = common if kind == "commitment" else common | {"boundary", "expected", "actual"}
            identity = tuple(record.get(key) for key in (
                "task_id", "task_revision", "attempt_id", "run_id"))
            action_id = record.get("action_id")
            if (kind not in ("commitment", "handoff") or set(record) != fields or
                    type(record["version"]) is not int or record["version"] != 1 or
                    identity != self._identity or type(record["task_revision"]) is not int or
                    not _within_committed_actions(self._dispatch_counter, action_id) or
                    type(record["generation"]) is not int or record["generation"] != action_id or
                    type(record["classification"]) is not str or
                    record["classification"] not in _CLASSIFICATIONS or
                    (record["slot_ref"] is not None and type(record["slot_ref"]) is not str)):
                raise ValueError("invalid evidence identity")
            if kind == "commitment":
                if action_id in self._commitments:
                    raise ValueError("duplicate commitment")
                if ((record["classification"] == "text" and record["slot_ref"] not in self._expected) or
                        (record["classification"] != "text" and record["slot_ref"] is not None)):
                    raise ValueError("invalid commitment slot")
                self._commitments[action_id] = record
                reference = self._reference(f"commitment:{action_id}")
                public = self._public_record(reference, kind="commitment", action_id=action_id,
                                             generation=record["generation"],
                                             classification=record["classification"],
                                             slot_ref=record["slot_ref"])
            else:
                commitment = self._commitments.get(action_id)
                if (action_id in self._handoffs or commitment is None or
                        commitment["classification"] != "text" or record["classification"] != "text" or
                        commitment["slot_ref"] != record["slot_ref"] or record["boundary"] != _BOUNDARY or
                        type(record["actual"]) is not str or len(record["actual"]) != 64 or
                        any(char not in "0123456789abcdef" for char in record["actual"]) or
                        type(record["expected"]) is not str or
                        not hmac.compare_digest(record["expected"], self._expected[record["slot_ref"]])):
                    raise ValueError("invalid handoff")
                self._handoffs[action_id] = record
                matched = hmac.compare_digest(record["actual"], record["expected"])
                reference = self._reference(f"handoff:{action_id}")
                public = self._public_record(reference, kind="handoff", action_id=action_id,
                                             generation=record["generation"],
                                             classification="text", slot_ref=record["slot_ref"],
                                             status="pass" if matched else "fail",
                                             source="authenticated_boundary_receipt")
            self._public[reference] = public
            return dict(public)
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
            self._invalid = True
            return None

    def drain(self) -> list[dict]:
        """Bounded nonblocking work; there is no total action/ledger size cap."""
        records = []
        self._drained_to_empty = False
        if not self._closed:
            for _ in range(_DRAIN_BATCH):
                try:
                    packet, _ancillary, flags, _address = self._receiver.recvmsg(_PACKET_BYTES)
                except BlockingIOError:
                    self._drained_to_empty = True
                    break
                except InterruptedError:
                    break
                except OSError:
                    self._unavailable.value = 1
                    break
                if flags & socket.MSG_TRUNC:
                    self._invalid = True
                    continue
                record = self._accept(packet)
                if record is not None:
                    records.append(record)
        self._failure_witness()
        return records

    def view(self, coverage: dict | None) -> _SlotEvidenceView:
        self.drain()
        witness = self._failure_witness()
        failures = tuple(self._reference(f"handoff:{action}")
                         for action, record in self._handoffs.items()
                         if not hmac.compare_digest(record["actual"], record["expected"]))
        if witness is not None or failures:
            return _SlotEvidenceView(
                self._identity, "fail", "Actual driver input differs from the accepted immutable slot.",
                time.time(), ((witness,) if witness is not None else ()) + failures)

        def unknown(reason):
            return _SlotEvidenceView(self._identity, "unknown", reason, None, ())

        if self._dispatch_counter is None:
            return unknown("Immutable-slot evidence is not bound to the runtime dispatch counter.")
        if self._invalid or self._unavailable.value or self._closed:
            return unknown("Private slot evidence is invalid, duplicated, lost, or unavailable.")
        if not self._drained_to_empty:
            return unknown("Private slot evidence has not yet been drained to its current end.")
        keys = ("dispatches", "text_dispatches", "nontext_dispatches", "unknown_dispatches", "generation")
        if (type(coverage) is not dict or
                any(type(coverage.get(key)) is not int or coverage[key] < 0 for key in keys) or
                type(coverage.get("sealed_generation")) is not int):
            return unknown("A synchronized immutable-slot coverage snapshot is unavailable.")
        generation, count = coverage["generation"], coverage["dispatches"]
        if coverage["sealed_generation"] != generation:
            return unknown("Dispatch generation is not sealed; later actions could invalidate coverage.")
        if (not count or generation != count or
                sum(coverage[key] for key in keys[1:4]) != count):
            return unknown("Complete supported dispatch coverage is not established.")
        if coverage["unknown_dispatches"]:
            return unknown("At least one committed action has an unsupported or unknown boundary.")
        if (len(self._commitments) != count or
                any(action < 1 or action > count for action in self._commitments)):
            return unknown("Authenticated action commitments do not cover the sealed generation.")
        totals = {kind: 0 for kind in _CLASSIFICATIONS}
        for record in self._commitments.values():
            totals[record["classification"]] += 1
        if (totals["text"] != coverage["text_dispatches"] or
                totals["nontext"] != coverage["nontext_dispatches"] or
                totals["unknown"] != coverage["unknown_dispatches"] or
                len(self._handoffs) != totals["text"]):
            return unknown("Authenticated text handoffs or action classifications are incomplete.")
        reference = self._reference(f"coverage:{generation}")
        self._public[reference] = self._public_record(
            reference, kind="sealed_coverage", generation=generation,
            dispatches=count, text_dispatches=totals["text"],
            nontext_dispatches=totals["nontext"], unknown_dispatches=0,
            sealed_generation=generation, status="pass")
        refs = (reference,) + tuple(self._reference(f"handoff:{action}") for action in self._handoffs)
        return _SlotEvidenceView(
            self._identity, "pass", "Every sealed committed action has supported immutable-slot evidence.",
            time.time(), refs)

    def export(self) -> list[dict]:
        """Copies of retained redacted records; never authentication material."""
        # Ingesting here could discover a late failure/duplicate after the final
        # assessment. The supervisor drains and assesses before exporting.
        return [dict(record) for record in self._public.values()]

    def close(self) -> None:
        self._receiver.close()
        self.sink.close()
        self._closed = True
