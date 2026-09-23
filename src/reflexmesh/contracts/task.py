"""Strict routing-only task contract. Capabilities are declarations, not probes."""
from dataclasses import dataclass
from enum import Enum


class ValidationError(ValueError):
    """Input does not satisfy the public contract."""


class Route(str, Enum):
    CUA = "CUA"
    LLM = "LLM"
    PERCEPTION = "PERCEPTION"


def validate_routes(value: object, field: str) -> None:
    if type(value) is not tuple or any(type(r) is not Route for r in value):
        raise ValidationError(f"{field} must be a tuple of Route values")
    if len(set(value)) != len(value):
        raise ValidationError(f"{field} must contain unique routes")


def validate_text(value: object, field: str, limit: int) -> None:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise ValidationError(f"{field} must be a nonblank string of at most {limit} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{field} must contain valid Unicode") from exc


@dataclass(frozen=True)
class Task:
    schema_version: str
    task_id: str
    goal: str
    capabilities: tuple[Route, ...]
    allowed_routes: tuple[Route, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != "0.1":
            raise ValidationError('schema_version must be "0.1"')
        validate_text(self.task_id, "task_id", 128)
        validate_text(self.goal, "goal", 10000)
        validate_routes(self.capabilities, "capabilities")
        validate_routes(self.allowed_routes, "allowed_routes")

    @classmethod
    def from_dict(cls, data: object) -> "Task":
        fields = {"schema_version", "task_id", "goal", "capabilities", "allowed_routes"}
        if type(data) is not dict or set(data) != fields:
            raise ValidationError("Task must be an object with exactly the required fields")
        parsed = dict(data)
        for field in ("capabilities", "allowed_routes"):
            values = data[field]
            if type(values) is not list or any(type(v) is not str for v in values):
                raise ValidationError(f"{field} must be an array of route strings")
            try:
                parsed[field] = tuple(Route(v) for v in values)
            except ValueError as exc:
                raise ValidationError(f"{field} contains an unknown route") from exc
        return cls(**parsed)
