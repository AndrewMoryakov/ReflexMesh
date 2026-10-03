"""Deterministic fixture selector; entries name stable fixture targets, never raw indices.

Besides ordinary decisions, a script can model a misbehaving provider for acceptance checks:
`force: "unoffered"` proposes a fixture target that was not offered, `index` proposes an unknown
element, `force: "slot"` substitutes an undeclared slot reference, and `refuse` answers with low
confidence so the harness gate refuses. Runtime admission must stop all of them before dispatch.
"""

LOW = 0.3


class FixtureScriptProvider:
    def __init__(self, entries: list[dict]):
        from systemone_harness.provider import ScriptProvider

        self.base = ScriptProvider([])
        self.entries = list(entries)
        self.calls = 0
        self.environment = None

    def bind(self, environment):
        """The adapter whose full target map resolves forced (unoffered) proposals."""
        self.environment = environment

    def _forced_index(self, target_id: str) -> str:
        targets = getattr(self.environment, "targets", None) or {}
        matching = [index for index, target in targets.items() if target[1] == target_id]
        if len(matching) != 1:
            raise ValueError("forced script target is not present on the page")
        return matching[0]

    def decide(self, state, questions):
        entry = self.entries[self.calls] if self.calls < len(self.entries) else {"action": "finish"}
        self.calls += 1
        action = entry["action"]
        forced = None
        if action in ("finish", "refuse"):
            script = "finish"
            if action == "refuse":
                offered = questions.get("next_action", {}).get("criteria") or {}
                choice = next((name for name in offered if name not in ("finish", "escalate")), None)
                if choice is None:
                    raise ValueError("no action to refuse")
                script = choice
        else:
            field = "element" if action == "click" else "field"
            if "index" in entry:
                forced = entry["index"]
            elif entry.get("force") == "unoffered":
                forced = self._forced_index(entry["target_id"])
            targets = questions.get(f"{action}__{field}", {}).get("criteria") or {}
            if forced is None:
                matching = [str(index) for index, description in targets.items()
                            if f"[reflex:{entry['target_id']}]" in str(description)]
                if len(matching) != 1:
                    raise ValueError("script target is not an admissible candidate")
                params = [f"{field}='{matching[0]}'"]
            else:
                params = []
            if action == "type_text":
                values = questions.get("type_text__value", {}).get("criteria") or {}
                if entry["slot"] not in values and entry.get("force") != "slot":
                    raise ValueError("script slot is not an admissible candidate")
            script = f"{action}({', '.join(params)})"
        self.base.actions = [script]
        self.base.calls = 0
        decision = self.base.decide(state, questions)
        if action == "refuse":
            answer = decision.answers["next_action"]
            answer["confidence"] = LOW
            answer["probabilities"] = {key: (LOW if key == answer["choice"] else 0.0)
                                       for key in answer.get("probabilities", {})}
        if forced is not None:
            field = "element" if action == "click" else "field"
            decision.answers[f"{action}__{field}"] = {"type": "choice", "choice": forced,
                                                      "probabilities": {}, "confidence": 0.99}
        if action == "type_text":
            slot = entry["slot"]
            answer = decision.answers["type_text__value"]
            answer["choice"] = slot
            answer["probabilities"] = {key: (0.99 if key == slot else 0.0) for key in values}
            answer["confidence"] = 0.99
        return decision


def validate_script(raw):
    if type(raw) is not list or not raw or len(raw) > 32:
        raise ValueError("script must be a nonempty list of at most 32 entries")
    for row in raw:
        if type(row) is not dict or row.get("action") not in ("click", "type_text", "finish", "refuse"):
            raise ValueError("invalid script action")
        if row["action"] in ("finish", "refuse"):
            allowed = [{"action"}]
        elif row["action"] == "click":
            allowed = [{"action", "target_id"}, {"action", "target_id", "force"}, {"action", "index"}]
        else:
            allowed = [{"action", "target_id", "slot"}, {"action", "target_id", "slot", "force"}]
        if set(row) not in allowed or any(type(v) is not str or not v for v in row.values()):
            raise ValueError("invalid script entry")
        if "force" in row and row["force"] != ("unoffered" if row["action"] == "click" else "slot"):
            raise ValueError("invalid script force")
        if "index" in row and not row["index"].isdecimal():
            raise ValueError("invalid script index")
    return raw
