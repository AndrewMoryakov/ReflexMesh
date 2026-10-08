"""Runtime-owned completion assessments; missing contract evidence is not success."""

from __future__ import annotations

import math
import time

from reflexmesh.contracts.execution import ExecutionTask
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
    "runtime.permissions": "Full target attributes and dispatch-time policy revision are not evidenced.",
    "runtime.immutable_slots": "Resolved slot values are not evidenced across the adapter dispatch boundary.",
    "runtime.ownership": "Exclusive fixture claim and browser session/owner identity are not evidenced.",
    "runtime.dispatch_ordering": "Shared gate ordering and observation/policy identity are not persisted.",
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
                       ownership: FixtureOwnership | None = None) -> list[dict]:
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
