"""Handoff (INV-12): what a client needs to decide the next subtask after a non-completed attempt.

A handoff returns control. It is never completion of the goal, it grants no permission, and the
attempt that produced it has no authority left. Slot values are never included, only references.
"""

from __future__ import annotations

from reflexmesh.contracts.execution import ExecutionTask

OUTCOMES = {"applied": "done", "not_applied": "not_done", "unknown": "possibly_done"}
# Effects confined to the attempt's own browser session, which is discarded after every attempt.
SESSION_LOCAL = {"navigate", "type_text", "toggle_setting"}
KINDS = {"blocked": "blocked", "incomplete": "incomplete", "cancelled": "cancelled", "failed": "failed"}


def standalone_chain(attempt_id: str) -> dict:
    return {"root_attempt_id": attempt_id, "parent_attempt_id": None, "sequence": 1}


def remaining_budget(root_limits: dict, used: dict) -> dict:
    """Chain-level remainder: root limits minus everything every attempt in the chain used."""
    return {"steps": max(0, int(root_limits["max_steps"]) - int(used.get("steps", 0))),
            "model_calls": max(0, int(root_limits["max_model_calls"]) - int(used.get("model_calls", 0))),
            "wall_seconds": max(0.0, round(float(root_limits["wall_seconds"]) - float(used.get("wall_seconds", 0.0)), 3))}


def chain_usage(prior: dict | None, result: dict) -> dict:
    """Usage of the chain up to and including this attempt."""
    prior = prior or {}
    budget = result.get("budget") or {}
    return {"steps": int(prior.get("steps", 0)) + int(budget.get("steps", 0)),
            "model_calls": int(prior.get("model_calls", 0)) + int(budget.get("model_calls", 0)),
            "wall_seconds": round(float(prior.get("wall_seconds", 0.0)) + float(budget.get("elapsed_seconds", 0.0)), 3)}


def content_gaps(result: dict) -> list[str]:
    """Required text fields that were empty in the attempt's last browser observation."""
    observations = [row["data"][0] for row in result.get("trace", [])
                    if row.get("kind") == "observation" and row.get("data") and type(row["data"][0]) is dict]
    if not observations:
        return []
    gaps = observations[-1].get("content_gaps")
    return sorted(g for g in gaps if type(g) is str) if type(gaps) is list else []


def build_handoff(task: ExecutionTask, result: dict, *, root_limits: dict, usage: dict) -> dict | None:
    if result.get("attempt_status") == "completed":
        return None
    actions = []
    for row in result.get("actions", []):
        item = {"id": row.get("id"), "operation": row.get("operation"), "target_id": row.get("target_id"),
                "outcome": OUTCOMES.get(row.get("effect"), "possibly_done")}
        if row.get("slot_ref"):
            item["slot_ref"] = row["slot_ref"]
        actions.append(item)
    # Only an operation with an external effect can make a continuation a blind repeat (INV-11).
    unresolved = [a for a in actions if a["operation"] not in SESSION_LOCAL and a["outcome"] == "possibly_done"]
    gaps = content_gaps(result)
    if unresolved:
        kind = "unknown_effect"
    elif gaps:
        kind = "needs_content"
    else:
        kind = KINDS.get(result.get("attempt_status"), "failed")
    remaining = remaining_budget(root_limits, usage)
    exhausted = remaining["steps"] < 1 or remaining["model_calls"] < 1 or remaining["wall_seconds"] < 1
    continuation = ({"allowed": False, "reason": "unresolved_effect"} if unresolved else
                    {"allowed": False, "reason": "budget_exhausted"} if exhausted else
                    {"allowed": True, "reason": None})
    return {
        "schema_version": "handoff/0.1",
        "kind": kind,
        "task": {"task_id": task.task_id, "revision": task.revision, "goal": task.goal},
        "attempt": {"attempt_id": result.get("attempt_id"), "executor_id": result.get("executor_id"),
                    **(result.get("chain") or {})},
        "stop": {k: result.get(k) for k in ("attempt_status", "stop_reason", "task_outcome")},
        "attempted": any(a["operation"] != "navigate" for a in actions),
        "actions": actions,
        "verification": [{"id": r.get("id"), "kind": r.get("kind"), "status": r.get("status")}
                         for r in result.get("verification", [])],
        "needs": [{"kind": "content", "target_id": gap, "reason": "required_field_empty_without_slot"}
                  for gap in gaps] if kind == "needs_content" else [],
        "remaining_budget": remaining,
        "constraints": {"permissions": sorted(task.permissions), "allowed_executors": list(task.allowed_executors),
                        "text_slot_refs": [s.reference for s in task.slots], "start_path": task.start_path,
                        "origin": task.origin},
        "trace_ref": result.get("trace_ref"),
        "evidence_refs": list(result.get("evidence_refs") or []),
        "continuation": continuation,
    }
