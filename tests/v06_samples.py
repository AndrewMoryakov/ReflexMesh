"""Shared builders for the V0.6 browser tests."""

import copy

PERMISSIONS = ["navigate", "click", "type_text", "select_option", "scroll", "go_back", "switch_tab", "wait"]

BASE = {
    "schema_version": "browser-task/0.1",
    "task_id": "order-note-125",
    "revision": 1,
    "goal": "Find order 125, add the prepared note to it and save the note.",
    "start_url": "http://127.0.0.1:8770/orders",
    "allowed_origins": ["http://127.0.0.1:8770"],
    "permissions": PERMISSIONS,
    "constraints": [],
    "text_slots": [
        {"id": "order", "version": 1, "value": "125", "sensitivity": "normal"},
        {"id": "note", "version": 1, "value": "Перезвонить завтра", "sensitivity": "normal"},
    ],
    "criteria": [{"id": "note_saved", "predicate": "revisit_match",
                  "args": {"anchor_slot_ref": "order@1", "field_name": "Notes", "expected_slot_ref": "note@1"}}],
    "limits": {"wall_seconds": 120, "max_steps": 64, "max_model_calls": 96, "max_action_retries": 0,
               "max_handoffs": 3},
}


def task_dict(**changes) -> dict:
    data = copy.deepcopy(BASE)
    data.update(copy.deepcopy(changes))
    return data


# Section 16, F12 "each completed has evidence", held on every path rather than one: while a browser test runs, every
# result BrowserRun returns as `completed` is checked against section 9's rule for it.
COMPLETED_CHECKED: list[str] = []


def completed_problems(result: dict, trace_seqs=None) -> list[str]:
    """Why a `completed` result is not evidenced (empty when it is, or when it is not `completed`). Section 9:
    completed only when every required criterion and constraint passes, with evidence, no essential effect is left
    unresolved, and the deadline holds."""
    if result.get("attempt_status") != "completed":
        return []
    problems = []
    if result.get("task_outcome") != "pass":
        problems.append(f"task_outcome {result.get('task_outcome')}")
    rows = result.get("verification") or []
    if not rows:
        problems.append("no verification rows")
    prefix = f"browser:{result.get('attempt_id')}:"
    for row in rows:
        refs = row.get("evidence_refs") or []
        if row.get("status") != "pass":
            problems.append(f"{row.get('id')}: {row.get('status')}")
        if not refs:
            problems.append(f"{row.get('id')}: no evidence_refs")
        for ref in refs:
            if trace_seqs is not None and ref.startswith(prefix) and int(ref[len(prefix):]) not in trace_seqs:
                problems.append(f"{row.get('id')}: {ref} is not in the trace")
    for action in result.get("actions") or []:
        if action.get("effect") == "unknown":
            problems.append(f"action {action.get('id')} {action.get('operation')}: effect unknown")
    if result.get("execution_outcome") == "unknown":
        problems.append("execution_outcome unknown")
    budget = result.get("budget") or {}
    wall = (budget.get("limits") or {}).get("wall_seconds")
    if wall is not None and (budget.get("elapsed_seconds") or 0) > wall + 0.25:
        problems.append(f"elapsed {budget.get('elapsed_seconds')} s past the {wall} s limit")
    if not result.get("evidence_refs"):
        problems.append("no runtime evidence_refs")
    return problems


def check_every_completion():
    """Wrap BrowserRun._finish so that every completed result it returns is checked; returns the undo."""
    from reflexmesh.runtime.browser_run import BrowserRun

    original = BrowserRun._finish

    def finish(self, started, notes):
        result = original(self, started, notes)
        if result.get("attempt_status") == "completed":
            COMPLETED_CHECKED.append(result.get("attempt_id"))
            problems = completed_problems(result, {r["seq"] for r in self.journal.records})
            if problems:
                raise AssertionError("completed without evidence: " + "; ".join(problems))
        return result

    BrowserRun._finish = finish
    return lambda: setattr(BrowserRun, "_finish", original)
