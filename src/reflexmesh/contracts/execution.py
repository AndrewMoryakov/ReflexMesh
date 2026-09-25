"""V0.5 execution input; routing-only Task 0.1 remains a separate contract."""

from __future__ import annotations

import math
from dataclasses import dataclass
from urllib.parse import urlsplit

from .task import ValidationError, validate_text

PERMISSIONS = frozenset({"navigate", "type_text", "toggle_setting", "save_settings",
                         "submit_form", "start_export", "delete_account"})
PREDICATES = {
    "current_page": {"path": str, "target_id": str},
    "settings_saved": {"notify_email": bool},
    "form_submitted_once": {"name_slot": str, "email_slot": str},
    "export_completed_once": {},
    "account_intact": {},
}


def _object(value: object, fields: set[str], name: str) -> dict:
    if type(value) is not dict or set(value) != fields:
        raise ValidationError(f"{name} must have exactly {sorted(fields)}")
    return value


def _integer(value: object, name: str, *, zero: bool = False) -> int:
    if type(value) is not int or (value < 0 if zero else value <= 0):
        raise ValidationError(f"{name} must be a {'nonnegative' if zero else 'positive'} integer")
    return value


def _ident(value: object, name: str) -> str:
    validate_text(value, name, 128)
    if "@" in value:
        raise ValidationError(f"{name} cannot contain @")
    return value


def _path(value: object, name: str) -> str:
    if (type(value) is not str or not value.startswith("/") or "//" in value or
            "?" in value or "#" in value or "\\" in value or
            any(part in (".", "..") for part in value.split("/"))):
        raise ValidationError(f"{name} must be an absolute fixture path")
    return value


@dataclass(frozen=True)
class TextSlot:
    id: str
    version: int
    value: str

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.version}"


@dataclass(frozen=True)
class Criterion:
    id: str
    predicate: str
    args: dict


@dataclass(frozen=True)
class Limits:
    wall_seconds: float
    max_steps: int
    max_model_calls: int
    max_action_retries: int


@dataclass(frozen=True)
class ExecutionTask:
    task_id: str
    revision: int
    goal: str
    allowed_executors: tuple[str, ...]
    origin: str
    run_id: str
    start_path: str
    permissions: frozenset[str]
    slots: tuple[TextSlot, ...]
    criteria: tuple[Criterion, ...]
    limits: Limits

    @classmethod
    def from_dict(cls, data: object) -> "ExecutionTask":
        required = {"schema_version", "task_id", "revision", "goal", "allowed_executors",
                    "fixture", "start_path", "permissions", "text_slots", "criteria", "limits"}
        d = _object(data, required, "execution task")
        if d["schema_version"] != "execution-task/0.1":
            raise ValidationError("unsupported execution task schema")
        task_id = _ident(d["task_id"], "task_id")
        revision = _integer(d["revision"], "revision")
        validate_text(d["goal"], "goal", 10000)

        executors = d["allowed_executors"]
        if (type(executors) is not list or not executors or
                any(type(v) is not str or not v.strip() for v in executors) or
                len(set(executors)) != len(executors)):
            raise ValidationError("allowed_executors must have unique nonblank IDs")

        fixture = _object(d["fixture"], {"origin", "run_id"}, "fixture")
        origin = fixture["origin"]
        try:
            parts = urlsplit(origin)
            valid = (type(origin) is str and parts.scheme == "http" and parts.hostname == "127.0.0.1"
                     and parts.netloc == f"127.0.0.1:{parts.port}" and parts.port is not None
                     and 1 <= parts.port <= 65535 and not parts.path and not parts.query
                     and not parts.fragment and not parts.username and not parts.password)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValidationError("fixture origin must be http://127.0.0.1:PORT")
        run_id = _ident(fixture["run_id"], "fixture.run_id")
        start_path = _path(d["start_path"], "start_path")

        permissions = d["permissions"]
        if (type(permissions) is not list or any(type(p) is not str or p not in PERMISSIONS for p in permissions)
                or len(set(permissions)) != len(permissions)):
            raise ValidationError("permissions must contain unique supported operation names")

        raw_slots = d["text_slots"]
        if type(raw_slots) is not list:
            raise ValidationError("text_slots must be a list")
        slots = []
        for raw in raw_slots:
            s = _object(raw, {"id", "version", "value"}, "text slot")
            ref = _ident(s["id"], "slot id")
            version = _integer(s["version"], "slot version")
            if type(s["value"]) is not str:
                raise ValidationError("slot value must be a string")
            slots.append(TextSlot(ref, version, s["value"]))
        if len({s.reference for s in slots}) != len(slots):
            raise ValidationError("duplicate text slot reference")

        raw_criteria = d["criteria"]
        if type(raw_criteria) is not list or not raw_criteria:
            raise ValidationError("criteria must be a nonempty list")
        criteria = []
        for raw in raw_criteria:
            c = _object(raw, {"id", "kind", "predicate", "args"}, "criterion")
            cid = _ident(c["id"], "criterion id")
            if c["kind"] != "postcondition" or type(c["predicate"]) is not str:
                raise ValidationError("unsupported criterion kind")
            args = c["args"]
            if type(args) is not dict:
                raise ValidationError("criterion args must be an object")
            spec = PREDICATES.get(c["predicate"])
            if spec is not None:
                if set(args) != set(spec) or any(type(args[k]) is not t for k, t in spec.items()):
                    raise ValidationError("invalid predicate arguments")
                for key, val in args.items():
                    if key.endswith("_slot") and val not in {s.reference for s in slots}:
                        raise ValidationError("unknown slot in criterion")
                    if key == "path":
                        _path(val, "criterion path")
            criteria.append(Criterion(cid, c["predicate"], dict(args)))
        if len({c.id for c in criteria}) != len(criteria):
            raise ValidationError("duplicate criterion id")

        raw_limits = _object(d["limits"], {"wall_seconds", "max_steps", "max_model_calls",
                                            "max_action_retries"}, "limits")
        wall = raw_limits["wall_seconds"]
        if type(wall) not in (int, float) or not math.isfinite(wall) or wall <= 0:
            raise ValidationError("wall_seconds must be positive and finite")
        steps = _integer(raw_limits["max_steps"], "max_steps")
        calls = _integer(raw_limits["max_model_calls"], "max_model_calls")
        retries = _integer(raw_limits["max_action_retries"], "max_action_retries", zero=True)
        if retries != 0:
            raise ValidationError("automatic action retries are unsupported")
        return cls(task_id, revision, d["goal"], tuple(executors), origin, run_id, start_path,
                   frozenset(permissions), tuple(slots), tuple(criteria), Limits(float(wall), steps, calls, retries))
