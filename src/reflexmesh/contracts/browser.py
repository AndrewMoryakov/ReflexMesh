"""browser-task/0.1: a bounded browser subtask for a site with no prepared scenario.

This contract lives beside `execution-task/0.1` and does not change it. There is no
`fixture` field: the page is whatever `start_url` serves, and nothing here names a
site-specific ID. Provider, model, keys and adapter version are service configuration,
never task input (V0.6 section 5).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from urllib.parse import urlsplit

from .execution import _ident, _integer, _object
from .task import ValidationError, validate_text

SCHEMA_VERSION = "browser-task/0.1"
MAX_INPUT_BYTES = 64 * 1024
MAX_SLOT_VALUE = 10_000

# Primitive operations a caller can grant. `focus_region`, `finish` and `escalate` are
# control operations of the loop and are deliberately not grantable.
PERMISSIONS = frozenset({"navigate", "click", "type_text", "select_option", "press_key",
                         "scroll", "go_back", "switch_tab", "wait"})
# Operations that cannot change anything outside the page view; the others may reach a server.
READ_OPERATIONS = frozenset({"scroll", "switch_tab", "wait"})
# A separate bit table from the fixture's: the gate takes its vocabulary from the task.
OPERATION_BITS = {name: 1 << index for index, name in enumerate(sorted(PERMISSIONS))}

# Criteria whose arguments are structurally defined by V0.6 section 11. Accepting the
# structure is not the same as having a verifier: see `BrowserTask.unsupported_criteria`.
CRITERION_ARGS = {
    "page_match": {"anchor_slot_ref": str, "field_name": str, "expected_slot_ref": str},
    "scoped_extract": {"anchor_slot_ref": str, "field_name": str, "output_key": str},
    "revisit_match": {"anchor_slot_ref": str, "field_name": str, "expected_slot_ref": str},
    "registered_check": {"check_id": str, "subject": str, "expected_slot_refs": list},
}
# `registered_check` ids known without a service checks file: none. A task cannot add one (section 11).
REGISTERED_CHECKS: frozenset[str] = frozenset()
LIMIT_FIELDS = {"wall_seconds", "max_steps", "max_model_calls", "max_action_retries", "max_handoffs"}
SENSITIVITIES = ("normal", "secret")


def canonical_origin(url: str) -> str | None:
    """`scheme://host[:port]` in the one spelling this contract accepts, else None."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except (TypeError, ValueError):
        return None
    if (type(url) is not str or parts.scheme not in ("http", "https") or not parts.hostname or
            parts.username is not None or parts.password is not None):
        return None
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    default = {"http": 80, "https": 443}[parts.scheme]
    return f"{parts.scheme}://{host}" + ("" if port in (None, default) else f":{port}")


def _reject_constant(name):
    raise ValidationError(f"{name} is not valid JSON")


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("duplicate key in JSON input")
        result[key] = value
    return result


def loads_strict(raw) -> object:
    """JSON text or UTF-8 bytes, at most 64 KiB, with no duplicate keys or NaN/Infinity."""
    if type(raw) is str:
        try:
            data = raw.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValidationError("input must be valid Unicode") from exc
    elif type(raw) is bytes:
        data = raw
    else:
        raise ValidationError("input must be text or bytes")
    if len(data) > MAX_INPUT_BYTES:
        raise ValidationError("input exceeds 64 KiB")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                          parse_constant=_reject_constant)
    except ValidationError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValidationError("input is not valid JSON") from exc


@dataclass(frozen=True)
class BrowserTextSlot:
    id: str
    version: int
    value: str
    sensitivity: str

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.version}"

    def __repr__(self):  # a value must not leak through a log line
        return f"BrowserTextSlot(reference={self.reference!r}, sensitivity={self.sensitivity!r})"


@dataclass(frozen=True)
class BrowserCriterion:
    id: str
    predicate: str
    args: dict


@dataclass(frozen=True)
class BrowserLimits:
    """Every limit is finite: the unlimited mode of the fixture CLI does not carry over."""

    wall_seconds: float
    max_steps: int
    max_model_calls: int
    max_action_retries: int
    max_handoffs: int

    @classmethod
    def from_dict(cls, value: object) -> "BrowserLimits":
        raw = _object(value, LIMIT_FIELDS, "limits")
        wall = raw["wall_seconds"]
        try:
            valid = type(wall) in (int, float) and math.isfinite(wall) and wall > 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValidationError("wall_seconds must be positive and finite")
        retries = _integer(raw["max_action_retries"], "max_action_retries", zero=True)
        if retries != 0:
            raise ValidationError("automatic action retries are unsupported")
        return cls(float(wall), _integer(raw["max_steps"], "max_steps"),
                   _integer(raw["max_model_calls"], "max_model_calls"), retries,
                   _integer(raw["max_handoffs"], "max_handoffs", zero=True))


@dataclass(frozen=True)
class BrowserTask:
    task_id: str
    revision: int
    goal: str
    start_url: str
    allowed_origins: tuple[str, ...]
    permissions: frozenset[str]
    constraints: tuple[BrowserCriterion, ...]
    slots: tuple[BrowserTextSlot, ...]
    criteria: tuple[BrowserCriterion, ...]
    limits: BrowserLimits

    # The gate reads its operation vocabulary from the task, so a browser operation is
    # never counted as an unchecked commit against the fixture's table.
    operation_bits = OPERATION_BITS

    @property
    def slot_references(self) -> tuple[str, ...]:
        return tuple(slot.reference for slot in self.slots)

    @property
    def unsupported_criteria(self) -> tuple[str, ...]:
        """IDs of criteria this build cannot judge (`blocked/unsupported_criterion`): an undefined predicate, or
        a `registered_check` whose `check_id` names no check registered with the service."""
        return self.criteria_unsupported(REGISTERED_CHECKS)

    def criteria_unsupported(self, registered: frozenset) -> tuple[str, ...]:
        """The same, against the checks a service actually registered."""
        return tuple(c.id for c in self.criteria if c.predicate not in CRITERION_ARGS or
                     (c.predicate == "registered_check" and c.args.get("check_id") not in registered))

    @property
    def unsupported_constraints(self) -> tuple[str, ...]:
        """No constraint predicate is registered: a requested constraint cannot be enforced
        by code, so the task is `blocked/unsupported_constraint` rather than silently run."""
        return tuple(c.id for c in self.constraints)

    @classmethod
    def from_json(cls, raw) -> "BrowserTask":
        return cls.from_dict(loads_strict(raw))

    @classmethod
    def from_dict(cls, data: object) -> "BrowserTask":
        d = _object(data, {"schema_version", "task_id", "revision", "goal", "start_url",
                           "allowed_origins", "permissions", "constraints", "text_slots",
                           "criteria", "limits"}, "browser task")
        if d["schema_version"] != SCHEMA_VERSION:
            raise ValidationError("unsupported browser task schema")
        task_id = _ident(d["task_id"], "task_id")
        revision = _integer(d["revision"], "revision")
        validate_text(d["goal"], "goal", 10000)

        start_url = d["start_url"]
        if type(start_url) is not str or len(start_url) > 2048:
            raise ValidationError("start_url must be an absolute http or https URL")
        start_origin = canonical_origin(start_url)
        if start_origin is None or urlsplit(start_url).fragment:
            raise ValidationError("start_url must be an absolute http or https URL without credentials")

        origins = d["allowed_origins"]
        if type(origins) is not list or not origins or len(origins) > 32:
            raise ValidationError("allowed_origins must be a nonempty list")
        for origin in origins:
            if type(origin) is not str or canonical_origin(origin) != origin or urlsplit(origin).path:
                raise ValidationError("allowed_origins must be canonical origins such as https://host")
        if len(set(origins)) != len(origins):
            raise ValidationError("allowed_origins must be unique")
        if start_origin not in origins:
            raise ValidationError("start_url must belong to an allowed origin")

        permissions = d["permissions"]
        if (type(permissions) is not list or any(type(p) is not str or p not in PERMISSIONS for p in permissions)
                or len(set(permissions)) != len(permissions)):
            raise ValidationError("permissions must contain unique supported operation names")

        raw_slots = d["text_slots"]
        if type(raw_slots) is not list or len(raw_slots) > 64:
            raise ValidationError("text_slots must be a list")
        slots = []
        for raw in raw_slots:
            s = _object(raw, {"id", "version", "value", "sensitivity"}, "text slot")
            if type(s["value"]) is not str or len(s["value"]) > MAX_SLOT_VALUE:
                raise ValidationError("slot value must be a string of at most 10000 characters")
            try:
                s["value"].encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValidationError("slot value must be valid Unicode") from exc
            if s["sensitivity"] not in SENSITIVITIES:
                raise ValidationError("slot sensitivity must be normal or secret")
            slots.append(BrowserTextSlot(_ident(s["id"], "slot id"), _integer(s["version"], "slot version"),
                                         s["value"], s["sensitivity"]))
        references = {slot.reference for slot in slots}
        if len(references) != len(slots):
            raise ValidationError("duplicate text slot reference")

        constraints = cls._rules(d["constraints"], "constraints", references, allow_empty=True)
        criteria = cls._rules(d["criteria"], "criteria", references, allow_empty=False)
        if {c.id for c in constraints} & {c.id for c in criteria}:
            raise ValidationError("criterion and constraint IDs must not overlap")
        return cls(task_id, revision, d["goal"], start_url, tuple(origins), frozenset(permissions),
                   tuple(constraints), tuple(slots), tuple(criteria), BrowserLimits.from_dict(d["limits"]))

    @staticmethod
    def _rules(raw: object, name: str, references: set[str], *, allow_empty: bool) -> list[BrowserCriterion]:
        if type(raw) is not list or (not raw and not allow_empty) or len(raw) > 64:
            raise ValidationError(f"{name} must be a{'' if allow_empty else ' nonempty'} list")
        rules = []
        for item in raw:
            c = _object(item, {"id", "predicate", "args"}, name[:-1])
            rid = _ident(c["id"], f"{name} id")
            if rid.startswith("runtime."):
                raise ValidationError("runtime. IDs are reserved for required constraints")
            if type(c["predicate"]) is not str or not c["predicate"] or type(c["args"]) is not dict:
                raise ValidationError(f"invalid {name[:-1]}")
            spec = CRITERION_ARGS.get(c["predicate"])
            if spec is not None:
                args = c["args"]
                if set(args) != set(spec) or any(type(args[k]) is not t for k, t in spec.items()):
                    raise ValidationError("invalid predicate arguments")
                refs = [args[k] for k in args if k.endswith("_slot_ref")]
                refs += list(args.get("expected_slot_refs", ()))
                if any(type(ref) is not str or ref not in references for ref in refs):
                    raise ValidationError(f"unknown slot reference in {name[:-1]}")
                for key in ("field_name", "output_key", "check_id", "subject"):
                    if key in args:
                        validate_text(args[key], key, 256)
            rules.append(BrowserCriterion(rid, c["predicate"], dict(c["args"])))
        if len({r.id for r in rules}) != len(rules):
            raise ValidationError(f"duplicate {name[:-1]} id")
        return rules
