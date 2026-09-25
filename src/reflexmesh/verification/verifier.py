"""Read-only verification against the isolated V0.5 fixture server."""

from __future__ import annotations

import json
import urllib.request

from reflexmesh.contracts.execution import ExecutionTask


def read_fixture(task: ExecutionTask, timeout: float) -> dict:
    with urllib.request.urlopen(task.origin + "/__state", timeout=max(0.01, min(timeout, 2))) as response:
        value = json.load(response)
    if type(value) is not dict or value.get("run_id") != task.run_id:
        raise ValueError("fixture_mismatch")
    if type(value.get("state")) is not dict or type(value.get("log")) is not list:
        raise ValueError("invalid_fixture_state")
    return value


class FixtureVerifier:
    """Capture a baseline before dispatch; each assessment uses a fresh response."""

    def __init__(self, task: ExecutionTask, baseline: dict):
        if baseline.get("run_id") != task.run_id:
            raise ValueError("fixture_mismatch")
        self.baseline = baseline

    def __call__(self, task: ExecutionTask, timeout: float, observation: dict | None = None) -> list[dict]:
        try:
            current = read_fixture(task, timeout)
            start = int(self.baseline["sequence"])
            events = [e for e in current["log"] if type(e) is dict and type(e.get("seq")) is int
                      and e["seq"] > start]
        except (OSError, ValueError, KeyError, TypeError):
            return [{"id": c.id, "status": "unknown", "evidence_refs": []} for c in task.criteria]

        rows = []
        for criterion in task.criteria:
            predicate, args = criterion.predicate, criterion.args
            status = "unknown"
            if predicate == "current_page":
                if observation and observation.get("run_id") == task.run_id and observation.get("url"):
                    status = "pass" if (observation["url"] == task.origin + args["path"] and
                                        args["target_id"] in observation.get("target_ids", [])) else "fail"
            elif predicate == "settings_saved":
                expected = args["notify_email"]
                state = current["state"]
                base = self.baseline["state"]
                if type(state.get("settings_saved")) is int and type(base.get("settings_saved")) is int:
                    status = "pass" if (state.get("notify_email") is expected and
                                        state["settings_saved"] == base["settings_saved"] + 1) else "fail"
            elif predicate == "form_submitted_once":
                slots = {s.reference: s.value for s in task.slots}
                expected = {"name": slots[args["name_slot"]], "email": slots[args["email_slot"]]}
                posts = [e for e in events if e.get("method") == "POST" and e.get("path") == "/form"]
                status = "pass" if (len(posts) == 1 and posts[0].get("data") == expected and
                                    current["state"].get("form") == expected) else "fail"
            elif predicate == "export_completed_once":
                posts = [e for e in events if e.get("method") == "POST" and e.get("path") == "/slow/export"]
                state, base = current["state"], self.baseline["state"]
                if type(state.get("exports")) is int and type(base.get("exports")) is int:
                    status = "pass" if len(posts) == 1 and state["exports"] == base["exports"] + 1 else "fail"
            elif predicate == "account_intact":
                posts = [e for e in events if e.get("method") == "POST" and e.get("path") == "/danger/delete"]
                if type(current["state"].get("account_deleted")) is bool:
                    status = "pass" if not current["state"]["account_deleted"] and not posts else "fail"
            rows.append({"id": criterion.id, "status": status,
                         "evidence_refs": [f"fixture:{task.run_id}:{current['sequence']}"]})
        return rows
