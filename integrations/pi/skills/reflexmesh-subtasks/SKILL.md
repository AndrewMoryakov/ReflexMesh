---
name: reflexmesh-subtasks
description: Delegate a bounded browser subtask to ReflexMesh through its MCP tools (reflexmesh_submit, reflexmesh_status, reflexmesh_cancel, reflexmesh_continue) and act on its verified result or handoff. Use when a goal needs a controlled browser action with explicit acceptance criteria.
---

# ReflexMesh subtasks

ReflexMesh runs **one bounded subtask per attempt** under its own runtime: it checks permissions and budgets, drives the browser, and verifies the result against the criteria you send. You (the client) decompose the goal, prepare every piece of text, and decide what happens after a stop.

## Protocol

1. Build an `execution-task/0.1` object. Every field below is required and no other field is accepted:

   ```json
   {"schema_version": "execution-task/0.1", "task_id": "support-001", "revision": 1,
    "goal": "Send a support request with the prepared name and email.",
    "allowed_executors": ["browser.soh"],
    "fixture": {"origin": "http://127.0.0.1:8765", "run_id": "run-001"},
    "start_path": "/support",
    "permissions": ["navigate", "type_text", "submit_form"],
    "text_slots": [{"id": "name", "version": 1, "value": "Test User"},
                   {"id": "email", "version": 1, "value": "test@example.com"}],
    "criteria": [{"id": "sent", "kind": "postcondition", "predicate": "support_request_sent_once",
                  "args": {"name_slot": "name@1", "email_slot": "email@1"}}],
    "limits": {"wall_seconds": 120, "max_steps": 24, "max_model_calls": 32, "max_action_retries": 0}}
   ```

   Grant only the permissions the subtask needs (`navigate`, `type_text`, `toggle_setting`, `save_settings`, `submit_form`, `start_export`, `delete_account`). Slots carry the exact text you prepared; criteria refer to them as `id@version`. `kind` is always `postcondition`; predicates are `current_page {path, target_id}`, `settings_saved {notify_email}`, `form_submitted_once {name_slot, email_slot}`, `support_request_sent_once {name_slot, email_slot}`, `export_completed_once {}` and `account_intact {}`.
2. Call `reflexmesh_submit` with the task, `routing_provider` and `action_provider`. Keep the returned `attempt_id`.
3. Call `reflexmesh_status` with `wait_seconds: 50` until `state` is `terminal`.
4. Read `result`:
   - **Success only** when `attempt_status = completed` **and** `task_outcome = pass`.
   - Otherwise read `result.handoff`. It is a return of control, never a success.

## Acting on a handoff

- `handoff.actions[].outcome` is `done`, `not_done`, or `possibly_done`. **Never repeat a `possibly_done` action**, and never resubmit a task that would repeat one. `continuation.allowed` is false in that case; report it to the user instead.
- `kind = needs_content`: a required field (`handoff.needs[].target_id`) is empty and no prepared text exists for it. ReflexMesh never writes text itself. Prepare the content, then call `reflexmesh_continue` with `parent_attempt_id` and `changes.add_text_slots: [{"id": "...", "version": 1, "value": "..."}]`. A slot that already exists can only get a new version, never a new value.
- `kind = blocked`: read `stop.stop_reason` (`policy_denied`, `no_route`, `executor_unavailable`, ...). Do not try to widen permissions through a continuation; it can only narrow them. Explain the blocker or submit a new, separately justified task.
- `kind = cancelled` / `incomplete` / `failed`: check `verification` and `remaining_budget`; continue only if the next step is clear and the budget allows it.

`reflexmesh_continue` starts a linked attempt with a fresh browser and the chain's **remaining** budget; any change to the task creates a new revision.

## Cancelling

`reflexmesh_cancel` requests cancellation; the attempt's gate decides. An action that was already dispatched may still take effect: check `reflexmesh_status` afterwards and look at its outcome.

## Fixture scenarios (testing)

The local test site (`experiments/v05/site/server.py`) offers `/form` (`form_submitted_once`), `/settings` (`settings_saved`), and `/support` (`support_request_sent_once`, with a required message that is not part of the predicate arguments: it must be supplied as an extra slot).
