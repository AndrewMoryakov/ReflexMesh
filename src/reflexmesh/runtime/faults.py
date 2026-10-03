"""Acceptance fault injection for the V0.5 B/J gate (specification section 9: "use barriers").

Faults are explicit, validated, recorded in the result, and only change *when* something happens
(a cancellation, a revocation, a hang, a lost return); they never grant execution authority or
weaken a check. They are test instruments, not product configuration.
"""

from __future__ import annotations

import math
import os
import signal
import time

from reflexmesh.contracts.execution import PERMISSIONS
from reflexmesh.contracts.task import ValidationError

_TARGET = str
_INDEX = int
SPEC = {
    "cancel_on_decision": _INDEX,      # SIGINT the supervisor when micro decision N is requested
    "hold_decision": dict,             # {"at": N, "seconds": S}: answer arrives S seconds late
    "hang_provider_at": _INDEX,        # micro decision N never returns
    "provider_error_at": _INDEX,       # micro decision N raises a provider protocol error
    "revoke_on_commit": _TARGET,       # revoke this operation after revalidation, before commit
    "cancel_before_commit": _TARGET,   # cancel after revalidation of this target, before commit
    "cancel_after_commit": _TARGET,    # cancel after commit of this target, before the driver call
    "corrupt_budget_after_cancel": bool,  # with cancel_after_commit: a late constraint violation
    "replace_before_admit": _TARGET,   # replace this DOM node once between observation and revalidation
    "hold_driver": dict,               # {"target_id": T, "until": "deadline" | seconds}: late return
    "hang_driver_on": _TARGET,         # the driver call for T never returns
    "lose_return_on": _TARGET,         # the driver acts on T, but its return is lost (ok=False)
    "hang_close": bool,                # closing the browser never returns
}


def validate_faults(raw) -> dict:
    if type(raw) is not dict or set(raw) - set(SPEC):
        raise ValidationError("faults must be an object with known keys")
    for key, value in raw.items():
        kind = SPEC[key]
        if kind is _INDEX and (type(value) is not int or value < 1):
            raise ValidationError(f"{key} must be a positive integer")
        if kind is _TARGET and (type(value) is not str or not value):
            raise ValidationError(f"{key} must be a nonempty string")
        if kind is bool and type(value) is not bool:
            raise ValidationError(f"{key} must be a boolean")
        if kind is dict and type(value) is not dict:
            raise ValidationError(f"{key} must be an object")
    if "revoke_on_commit" in raw and raw["revoke_on_commit"] not in PERMISSIONS:
        raise ValidationError("revoke_on_commit must name a permission")
    hold = raw.get("hold_decision")
    if hold is not None and (set(hold) != {"at", "seconds"} or type(hold["at"]) is not int or hold["at"] < 1
                             or not _seconds(hold["seconds"])):
        raise ValidationError("hold_decision must be {at, seconds}")
    driver = raw.get("hold_driver")
    if driver is not None and (set(driver) != {"target_id", "until"} or type(driver["target_id"]) is not str
                               or not (driver["until"] == "deadline" or _seconds(driver["until"]))):
        raise ValidationError("hold_driver must be {target_id, until}")
    if raw.get("corrupt_budget_after_cancel") and "cancel_after_commit" not in raw:
        raise ValidationError("corrupt_budget_after_cancel requires cancel_after_commit")
    return dict(raw)


def _seconds(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= 600


def request_cancel(gate, wait: float = 2.0) -> bool:
    """Deliver SIGINT to the supervising CLI (the same path as Ctrl-C) and wait for acceptance."""
    os.kill(os.getppid(), signal.SIGINT)
    limit = time.monotonic() + wait
    while time.monotonic() < limit:
        if gate.terminal.value:
            return gate.terminal.value == 1
        time.sleep(0.005)
    return False


def sleep_forever():
    while True:
        time.sleep(3600)
