"""Replaceable execution-router boundary; task supervision stays outside routers.

Adapters declaring ``local`` must reserve every provider request (including each
stage and retry) immediately before sending it. ``opaque`` adapters cannot make
that guarantee: their internal usage is unknown and outside the local-call cap.
No router owns the whole-task deadline, cancellation or completion verification.
"""

from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from reflexmesh.contracts.task import Task
from reflexmesh.routing.jev_router import route_task as jev_route
from reflexmesh.routing.stub import route_task as stub_route


@dataclass(frozen=True)
class RouterCapabilities:
    model_call_accounting: Literal["none", "local", "opaque"]


@dataclass(frozen=True)
class RouterUsage:
    # None means unknown, never zero and never a proven finite upper bound.
    model_calls: int | None


@dataclass(frozen=True)
class RoutingOutcome:
    decision: dict
    usage: RouterUsage


class Router(Protocol):
    router_id: str
    capabilities: RouterCapabilities

    def route(self, task: Task, *, timeout: float,
              reserve_call: Callable[[], None]) -> RoutingOutcome: ...


class StubRouter:
    router_id = "stub"
    capabilities = RouterCapabilities("none")

    def route(self, task: Task, *, timeout: float,
              reserve_call: Callable[[], None]) -> RoutingOutcome:
        return RoutingOutcome(stub_route(task).to_dict(), RouterUsage(0))


class JevRouter:
    router_id = "jevrouter"
    capabilities = RouterCapabilities("opaque")

    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def route(self, task: Task, *, timeout: float,
              reserve_call: Callable[[], None]) -> RoutingOutcome:
        # One /route HTTP request can trigger several provider requests. The
        # current service offers no enforceable budget or complete usage API.
        return RoutingOutcome(jev_route(task, endpoint=self.endpoint, timeout=timeout),
                              RouterUsage(None))


def execution_router(name: str, endpoint: str) -> Router:
    if name == "stub":
        return StubRouter()
    if name == "jevrouter":
        return JevRouter(endpoint)
    raise ValueError("unsupported execution router")
