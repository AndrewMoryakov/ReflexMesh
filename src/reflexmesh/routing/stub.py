"""Fixed-order routing stub, deliberately independent of the goal text."""
from reflexmesh.contracts.decision import RoutingDecision
from reflexmesh.contracts.task import Route, Task


def route_task(task: Task) -> RoutingDecision:
    eligible = tuple(r for r in Route if r in task.capabilities and r in task.allowed_routes)
    if not eligible:
        return RoutingDecision(task.task_id, "abstained", None, (), "no_eligible_route")
    return RoutingDecision(task.task_id, "selected", eligible[0], eligible, "stub_fixed_order")
