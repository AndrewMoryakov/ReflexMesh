"""Deterministic fixture selector; entries name stable fixture targets, never raw indices."""


class FixtureScriptProvider:
    def __init__(self, entries: list[dict]):
        from systemone_harness.provider import ScriptProvider

        self.base = ScriptProvider([])
        self.entries = list(entries)
        self.calls = 0

    def decide(self, state, questions):
        entry = self.entries[self.calls] if self.calls < len(self.entries) else {"action": "finish"}
        self.calls += 1
        action = entry["action"]
        if action == "finish":
            script = "finish"
        else:
            field = "element" if action == "click" else "field"
            targets = questions.get(f"{action}__{field}", {}).get("criteria") or {}
            matching = [str(index) for index, description in targets.items()
                        if f"[reflex:{entry['target_id']}]" in str(description)]
            if len(matching) != 1:
                raise ValueError("script target is not an admissible candidate")
            params = [f"{field}='{matching[0]}'"]
            if action == "type_text":
                values = questions.get("type_text__value", {}).get("criteria") or {}
                if entry["slot"] not in values:
                    raise ValueError("script slot is not an admissible candidate")
            script = f"{action}({', '.join(params)})"
        self.base.actions = [script]
        self.base.calls = 0
        decision = self.base.decide(state, questions)
        if action == "type_text":
            slot = entry["slot"]
            answer = decision.answers["type_text__value"]
            answer["choice"] = slot
            answer["probabilities"] = {key: (0.99 if key == slot else 0.0) for key in values}
        return decision


def validate_script(raw):
    if type(raw) is not list or not raw or len(raw) > 32:
        raise ValueError("script must be a nonempty list of at most 32 entries")
    for row in raw:
        if type(row) is not dict or row.get("action") not in ("click", "type_text", "finish"):
            raise ValueError("invalid script action")
        fields = {"action"} if row["action"] == "finish" else ({"action", "target_id", "slot"}
                  if row["action"] == "type_text" else {"action", "target_id"})
        if set(row) != fields or any(type(v) is not str or not v for v in row.values()):
            raise ValueError("invalid script entry")
    return raw
