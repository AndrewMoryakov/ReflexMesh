"""V0.1 stub decision; never a claim of task execution."""
from dataclasses import dataclass
from .task import Route, ValidationError, validate_routes, validate_text


@dataclass(frozen=True)
class RoutingDecision:
    task_id: str
    status: str
    route: Route | None
    eligible_routes: tuple[Route, ...]
    reason_code: str

    def __post_init__(self) -> None:
        validate_text(self.task_id, "task_id", 128)
        validate_routes(self.eligible_routes, "eligible_routes")
        canonical = tuple(r for r in Route if r in self.eligible_routes)
        if self.eligible_routes != canonical:
            raise ValidationError("eligible_routes must use canonical order")
        if self.status == "selected":
            if (type(self.route) is not Route or not canonical or
                    self.route != canonical[0] or self.reason_code != "stub_fixed_order"):
                raise ValidationError("selected stub decision must choose the first eligible route")
        elif self.status == "abstained":
            if self.route is not None or canonical or self.reason_code != "no_eligible_route":
                raise ValidationError("abstained decision requires an empty eligible set")
        else:
            raise ValidationError("unknown routing status")

    def to_dict(self) -> dict:
        return {
            "schema_version": "0.1", "task_id": self.task_id,
            "status": self.status, "route": self.route.value if self.route else None,
            "eligible_routes": [r.value for r in self.eligible_routes],
            "reason_code": self.reason_code, "provider": "stub", "is_stub": True,
            "execution_performed": False, "confidence": None,
        }
