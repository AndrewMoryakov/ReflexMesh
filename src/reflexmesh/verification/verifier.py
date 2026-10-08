"""Read-only verification of effects bound by the controlled fixture to one claim."""

from __future__ import annotations

import json
import time
import urllib.request

from reflexmesh.contracts.execution import ExecutionTask
from reflexmesh.runtime.ownership import FixtureOwnership, OwnershipError


def _same_binding(value, expected):
    return (type(value) is dict and set(value) == set(expected) and
            all(type(value[key]) is type(item) and value[key] == item for key, item in expected.items()))


def read_fixture(task: ExecutionTask, timeout: float | None) -> dict:
    """Public health/identity read. Never a completion baseline or effect proof."""
    with urllib.request.urlopen(task.origin + "/__state", timeout=2.0 if timeout is None else
                                max(0.01, min(timeout, 2))) as response:
        value = json.load(response)
    if type(value) is not dict or value.get("run_id") != task.run_id:
        raise ValueError("fixture_mismatch")
    if type(value.get("state")) is not dict or type(value.get("log")) is not list:
        raise ValueError("invalid_fixture_state")
    return value


class FixtureVerifier:
    """An unclaimed public baseline cannot attribute another attempt's effects.

    The authenticated fixture response supplies both the atomic claim baseline
    and immutable effect bindings. The constructor's transported baseline is
    only a consistency hint; it is never accepted as independent evidence.
    """

    def __init__(self, task: ExecutionTask, baseline: dict, *, ownership=None):
        if baseline.get("run_id") != task.run_id:
            raise ValueError("fixture_mismatch")
        self.baseline = baseline
        self.ownership = ownership if type(ownership) is FixtureOwnership else None

    def __call__(self, task: ExecutionTask, timeout: float | None, observation: dict | None = None) -> list[dict]:
        unknown = [{"id": c.id, "kind": "postcondition", "status": "unknown",
                    "observed_at": None, "evidence_refs": []} for c in task.criteria]
        owner = self.ownership
        if owner is None or owner.identity_tuple[:2] != (task.task_id, task.revision) or owner.identity_tuple[3] != task.run_id:
            return unknown
        try:
            current = owner.read(timeout=timeout)
            observed_at = time.time()
            binding = current["binding"]
            baseline = current["baseline"]
            start = baseline["sequence"]
            if (current["phase"] not in ("active", "quarantined") or baseline != self.baseline or
                    type(start) is not int or type(current["sequence"]) is not int or
                    current["sequence"] < start or baseline["instance_id"] != binding["instance_id"] or
                    any(type(binding.get(k)) is not str or not binding[k] for k in ("session_id", "profile_id"))):
                return unknown
            events = [e for e in current["effects"] if type(e) is dict and type(e.get("seq")) is int
                      and e["seq"] > start]
            # Do not silently filter foreign/stale records and then assert a
            # passing count on a mixed or malformed source response.
            if (len(events) != len(current["effects"]) or
                    any(not _same_binding(e.get("binding"), binding) or e.get("method") != "POST" or
                        e.get("path") not in ("/settings", "/form", "/slow/export", "/danger/delete")
                        for e in events) or
                    len({e["seq"] for e in events}) != len(events) or
                    any(e["seq"] > current["sequence"] for e in events)):
                return unknown
        except (OwnershipError, OSError, ValueError, KeyError, TypeError):
            return unknown

        reference = (f"fixture:{task.run_id}:{binding['instance_id']}:"
                     f"{binding['claim_id']}:{binding['owner_epoch']}:{current['sequence']}")
        retained = {"ref": reference, "kind": "fixture_effect_snapshot", "ownership": dict(binding),
                    "source": "authenticated_fixture_response", "observed_at": observed_at,
                    "phase": current["phase"], "baseline_sequence": start,
                    "sequence": current["sequence"], "pending_effect_count": len(current["pending_effects"]),
                    "effects": [{key: event[key] for key in ("seq", "method", "path", "binding")}
                                for event in events]}
        rows = []
        for criterion in task.criteria:
            predicate, args = criterion.predicate, criterion.args
            status = "unknown"
            operations = []
            state, base = current["state"], baseline["state"]
            if predicate == "current_page":
                if (observation and _same_binding(observation.get("ownership"), binding) and
                        observation.get("run_id") == task.run_id and observation.get("url")):
                    status = "pass" if (observation["url"] == task.origin + args["path"] and
                                        args["target_id"] in observation.get("target_ids", [])) else "fail"
            elif predicate == "settings_saved":
                posts = [e for e in events if e.get("path") == "/settings"]
                if type(state.get("settings_saved")) is int and type(base.get("settings_saved")) is int:
                    status = "pass" if (len(posts) == 1 and state.get("notify_email") is args["notify_email"] and
                                        state["settings_saved"] == base["settings_saved"] + 1) else "fail"
                operations = ["save_settings"]
            elif predicate == "form_submitted_once":
                slots = {s.reference: s.value for s in task.slots}
                expected = {"name": slots[args["name_slot"]], "email": slots[args["email_slot"]]}
                posts = [e for e in events if e.get("path") == "/form"]
                status = "pass" if len(posts) == 1 and state.get("form") == expected else "fail"
                # A server POST proves submission, not a particular typing call
                # or the browser driver's internal action sequence.
                operations = ["submit_form"]
            elif predicate == "export_completed_once":
                posts = [e for e in events if e.get("path") == "/slow/export"]
                if type(state.get("exports")) is int and type(base.get("exports")) is int:
                    status = "pass" if len(posts) == 1 and state["exports"] == base["exports"] + 1 else "fail"
                operations = ["start_export"]
            elif predicate == "account_intact":
                posts = [e for e in events if e.get("path") == "/danger/delete"]
                if type(state.get("account_deleted")) is bool:
                    status = "pass" if not state["account_deleted"] and not posts else "fail"
            if current.get("pending_effects") and predicate != "current_page":
                status = "unknown"
            rows.append({"id": criterion.id, "kind": "postcondition", "status": status,
                         "observed_at": observed_at, "ownership": dict(binding),
                         "verified_operations": operations,
                         "fixture_snapshot": retained, "evidence_refs": [reference]})
        return rows
