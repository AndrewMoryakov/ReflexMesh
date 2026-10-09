"""Runtime-owned completion assessments; missing contract evidence is not success."""

from __future__ import annotations

import math
import time

from reflexmesh.contracts.execution import ExecutionTask, OPERATION_BITS, PERMISSIONS
from reflexmesh.tracing.slot_evidence import SlotEvidence
from reflexmesh.runtime.ownership import FixtureOwnership


# This set is not supplied by tasks, routers, workers, or postcondition verifiers.
REQUIRED_CONSTRAINTS = (
    "runtime.permissions",
    "runtime.immutable_slots",
    "runtime.ownership",
    "runtime.dispatch_ordering",
    "runtime.no_uncertain_repeats",
    "runtime.budget",
)

MISSING_EVIDENCE = {
    "runtime.permissions": "Not every dispatch commit was checked against the live permission mask.",
    "runtime.immutable_slots": "Resolved slot values are not evidenced across the adapter dispatch boundary.",
    "runtime.ownership": "Exclusive fixture claim and browser session/owner identity are not evidenced.",
    "runtime.dispatch_ordering": "Gate sequence numbers do not account for every commit, revocation and terminal transition.",
    "runtime.no_uncertain_repeats": "Adapter-internal single-shot execution is not established.",
    "runtime.budget": "A synchronized local counter snapshot is unavailable.",
}


def unknown_assessment(constraint_id: str) -> dict:
    return {"id": constraint_id, "kind": "execution_constraint", "status": "unknown",
            "reason": MISSING_EVIDENCE[constraint_id], "observed_at": None,
            "evidence_refs": [], **({"scope": "steps_and_locally_controlled_model_calls"}
                                   if constraint_id == "runtime.budget" else {})}


def assess_constraints(task: ExecutionTask, snapshot: dict | None, attempt_id: str,
                       slot_evidence: SlotEvidence | None = None,
                       coverage: dict | None = None,
                       ownership: FixtureOwnership | None = None,
                       dispatches: list | None = None,
                       revocations: list | None = None,
                       driver_outcomes: dict | None = None) -> list[dict]:
    """Assess only evidence currently available to the supervisor.

    Admission guards and settled effects do not establish full adapter contracts.
    In particular, local repeat guards cannot attest Browser Use's internal calls.
    Wall-deadline admission is separately serialized by AttemptGate.finish().
    """
    rows = [unknown_assessment(cid) for cid in REQUIRED_CONSTRAINTS]
    if type(ownership) is FixtureOwnership:
        view = ownership.view()
        if view.identity == (task.task_id, task.revision, attempt_id, task.run_id):
            rows[2].update(status=view.status, reason=view.reason,
                           observed_at=view.observed_at, evidence_refs=list(view.evidence_refs))
    # This capability is created by the supervisor. A dict supplied by worker
    # events or a postcondition verifier cannot install a constraint assertion.
    if type(slot_evidence) is SlotEvidence:
        view = slot_evidence.view(coverage)
        if view.identity == (task.task_id, task.revision, attempt_id, task.run_id):
            rows[1].update(status=view.status, reason=view.reason,
                           observed_at=view.observed_at, evidence_refs=list(view.evidence_refs))
    rows[0].update(_assess_permissions(task, snapshot, attempt_id, dispatches))
    rows[3].update(_assess_ordering(snapshot, attempt_id, dispatches, revocations))
    rows[4].update(_assess_repeats(attempt_id, dispatches, driver_outcomes))
    # Slot failures are independent of accounting availability. In particular,
    # a dead worker holding the budget lock must not erase a proven mismatch.
    if type(snapshot) is not dict or any(type(snapshot.get(key)) is not int or snapshot[key] < 0
                               for key in ("steps", "model_calls")):
        return rows
    limits = task.limits
    violated = ((limits.max_steps is not None and snapshot["steps"] > limits.max_steps) or
                (limits.max_model_calls is not None and snapshot["model_calls"] > limits.max_model_calls))
    rows[-1].update(status="fail" if violated else "pass", observed_at=time.time(),
                    reason="Local counters exceed an enabled limit." if violated else
                           "Observed local counters respect enabled limits; null limits are disabled.",
                    evidence_refs=[f"runtime:{attempt_id}:budget"])
    return rows


def _is_int(value) -> bool:
    return type(value) is int


def _assess_permissions(task: ExecutionTask, snapshot: dict | None, attempt_id: str,
                        dispatches: list | None) -> dict:
    """Permission enforcement for committed dispatches.

    A dispatch that names a known operation the task did not grant is a confirmed
    failure whatever else is available. A pass needs the gate's own record: every
    commit was checked against the live mask, and every operation the worker
    reported was among those the gate checked. Worker events alone never pass.
    """
    ref = [f"runtime:{attempt_id}:permissions"]
    if type(dispatches) is not list or any(type(row) is not dict for row in dispatches):
        return {}
    operations = [row.get("operation") for row in dispatches]
    granted = task.permissions
    if any(op in PERMISSIONS and op not in granted for op in operations):
        return {"status": "fail", "observed_at": time.time(), "evidence_refs": ref,
                "reason": "A dispatch named an operation the task did not grant."}
    if type(snapshot) is not dict or any(type(snapshot.get(key)) is not int or snapshot[key] < 0
                                         for key in ("dispatches", "committed_operations",
                                                     "unchecked_commits")):
        return {}
    if snapshot["unchecked_commits"]:
        return {"reason": "A dispatch was committed without a checked operation."}
    if len(operations) != snapshot["dispatches"] or any(op not in OPERATION_BITS for op in operations):
        return {"reason": "Reported dispatch operations do not match the gate's committed dispatches."}
    if any(not snapshot["committed_operations"] & OPERATION_BITS[op] for op in operations):
        return {"reason": "A reported operation was not among those the gate checked at commit."}
    return {"status": "pass", "observed_at": time.time(), "evidence_refs": ref,
            "reason": "Every committed dispatch was checked against the live permission mask."}


def _assess_ordering(snapshot: dict | None, attempt_id: str, dispatches: list | None,
                     revocations: list | None) -> dict:
    """Local order of proposals, commits, returns, revocations and the terminal transition.

    The supervisor assigns proposal/dispatch/return positions from the order in which it
    received events. Gate sequence numbers are issued under the gate lock to every commit,
    revocation and the first terminal transition. A pass needs the numbers reported by the
    worker, the supervisor's own revocation record and the terminal number to cover the
    gate's sequence exactly once each, so a reported number cannot be invented or moved.
    """
    ref = [f"runtime:{attempt_id}:dispatch_ordering"]
    if (type(dispatches) is not list or any(type(row) is not dict for row in dispatches) or
            type(revocations) is not list or any(type(row) is not dict for row in revocations)):
        return {}
    ordered = sorted(dispatches, key=lambda row: row["id"] if _is_int(row.get("id")) else -1)
    reported = [row.get("gate_seq") for row in ordered]
    valid_snapshot = (type(snapshot) is dict and
                      all(_is_int(snapshot.get(key)) and snapshot[key] >= 0
                          for key in ("dispatches", "seq", "terminal_seq")))
    if valid_snapshot:
        issued, terminal_seq = snapshot["seq"], snapshot["terminal_seq"]
        if any(_is_int(value) and (value > issued or (terminal_seq and value > terminal_seq))
               for value in reported):
            return {"status": "fail", "observed_at": time.time(), "evidence_refs": ref,
                    "reason": "A dispatch carries a gate number the gate did not issue before its terminal transition."}
    if not valid_snapshot:
        return {"reason": "A synchronized gate sequence snapshot is unavailable."}
    if (snapshot["dispatches"] != len(ordered) or
            any(not _is_int(row.get("id")) or not _is_int(row.get("gate_seq")) or
                not _is_int(row.get("dispatch_seq")) for row in ordered)):
        return {"reason": "Not every committed dispatch was reported with its gate and trace position."}
    if ([row["id"] for row in ordered] != list(range(1, len(ordered) + 1)) or
            any(b <= a for a, b in zip(reported, reported[1:])) or
            any(not _is_int(row.get("proposal_seq")) or row["proposal_seq"] >= row["dispatch_seq"] or
                (row.get("return_seq") is not None and
                 (not _is_int(row["return_seq"]) or row["return_seq"] <= row["dispatch_seq"]))
                for row in ordered)):
        return {"status": "fail", "observed_at": time.time(), "evidence_refs": ref,
                "reason": "Dispatches were not committed in sequence after their proposals."}
    revoked = [row.get("gate_seq") for row in revocations]
    unexplained = {"reason": "Gate sequence numbers are not each accounted for exactly once."}
    if any(not _is_int(value) for value in revoked):
        return unexplained
    accounted = sorted(reported + revoked + ([terminal_seq] if terminal_seq else []))
    if accounted != list(range(1, snapshot["seq"] + 1)):
        return unexplained
    return {"status": "pass", "observed_at": time.time(), "evidence_refs": ref,
            "reason": "Every gate sequence number is accounted for and dispatches follow their proposals in order."}


def _assess_repeats(attempt_id: str, dispatches: list | None, outcomes: dict | None) -> dict:
    """Repeated or post-uncertainty mutations seen at the controlled boundary.

    A second commit of one mutation, or any mutation committed after an earlier mutation
    returned an error or a failure, is a confirmed violation. Absence of such a sequence
    does not establish the row: the adapter's internal calls are not attested here, so a
    pass needs an authoritative record from the target itself.
    """
    if (type(dispatches) is not list or any(type(row) is not dict for row in dispatches) or
            type(outcomes) is not dict):
        return {}
    ordered = sorted(dispatches, key=lambda row: row["id"] if _is_int(row.get("id")) else -1)
    seen, uncertain = set(), False
    for row in ordered:
        if row.get("operation") == "navigate":
            continue
        key = (row.get("operation"), row.get("target_id"), row.get("slot_ref"))
        if uncertain or key in seen:
            return {"status": "fail", "observed_at": time.time(),
                    "evidence_refs": [f"runtime:{attempt_id}:no_uncertain_repeats"],
                    "reason": "A mutation was committed again, or after an earlier mutation had an uncertain effect."}
        seen.add(key)
        if outcomes.get(row.get("id")) in ("error", "not_ok"):
            uncertain = True
    return {"reason": "No repeated mutation was observed at the controlled boundary; "
                      "adapter-internal single-shot execution is not established."}


class ConstraintAssessments:
    """Fixed complete set with sticky confirmed failures, owned by the supervisor."""

    def __init__(self):
        self._rows = {cid: unknown_assessment(cid) for cid in REQUIRED_CONSTRAINTS}

    def update(self, assessments: list[dict], *, retain_passes: bool = False) -> None:
        # Treat omitted, duplicate, or unsupported rows as missing evidence.
        # Only internal assessors call this; worker/verifier assertions are not inputs.
        for cid in REQUIRED_CONSTRAINTS:
            if self._rows[cid]["status"] == "fail":
                continue
            matches = [row for row in assessments if type(row) is dict and row.get("id") == cid]
            row = matches[0] if len(matches) == 1 else None
            assessed = unknown_assessment(cid)
            if row is not None and row.get("kind") == "execution_constraint":
                refs, observed_at = row.get("evidence_refs"), row.get("observed_at")
                if (row.get("status") in ("pass", "fail") and
                        type(refs) is list and refs and all(type(ref) is str and ref for ref in refs) and
                        type(observed_at) in (int, float) and math.isfinite(observed_at) and
                        type(row.get("reason")) is str and row["reason"]):
                    assessed = {**assessed, **row, "evidence_refs": list(refs)}
                elif (row.get("status") == "unknown" and observed_at is None and refs == [] and
                      type(row.get("reason")) is str and row["reason"]):
                    # Preserve the trusted assessor's concrete missing-evidence
                    # reason without giving an unknown row any passing authority.
                    assessed["reason"] = row["reason"]
            # Once completion is accepted, a later missing observation does not
            # erase the evidence used then. A confirmed failure still overrides it.
            if retain_passes and self._rows[cid]["status"] == "pass" and assessed["status"] == "unknown":
                continue
            self._rows[cid] = assessed

    def all_pass(self) -> bool:
        return all(self._rows[cid]["status"] == "pass" for cid in REQUIRED_CONSTRAINTS)

    def has_fail(self) -> bool:
        return any(self._rows[cid]["status"] == "fail" for cid in REQUIRED_CONSTRAINTS)

    def rows(self) -> list[dict]:
        return [{**self._rows[cid], "evidence_refs": list(self._rows[cid]["evidence_refs"])}
                for cid in REQUIRED_CONSTRAINTS]
