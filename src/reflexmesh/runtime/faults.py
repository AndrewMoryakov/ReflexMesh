"""Acceptance fault injection for the V0.5 B/J gate (specification section 9: "use barriers").

Faults are explicit, validated, and recorded in the result. They perturb timing,
provider/driver outcomes, or fixture node identity; one opt-in negative test corrupts
a counter after cancellation to exercise sticky failure evidence. They never grant
execution authority or weaken a check. They are test instruments, not product configuration.
"""

from __future__ import annotations

import math
import os
import signal
import time
from copy import deepcopy
from pathlib import Path

from reflexmesh.contracts.execution import PERMISSIONS
from reflexmesh.contracts.task import ValidationError

_TARGET = str
_INDEX = int
SPEC = {
    "cancel_on_decision": _INDEX,      # SIGINT the supervisor when micro decision N is requested
    "hold_decision": dict,             # {"at": N, "seconds": S}: hold the pending decision request
    "hang_provider_at": _INDEX,        # micro decision N never returns
    "provider_error_at": _INDEX,       # micro decision N raises a provider protocol error
    "revoke_on_commit": _TARGET,       # revoke this operation after revalidation, before commit
    "cancel_before_commit": _TARGET,   # cancel after revalidation of this target, before commit
    "cancel_after_commit": _TARGET,    # cancel after commit of this target, before the driver call
    "corrupt_budget_after_cancel": bool,  # with cancel_after_commit: a late constraint violation
    "replace_before_admit": _TARGET,   # replace this DOM node once between observation and revalidation
    "hold_driver": dict,               # {"target_id": T, "until": "deadline" | seconds}: late return
    "hang_driver_on": _TARGET,         # withhold the driver return for T indefinitely
    "lose_return_on": _TARGET,         # the driver acts on T, but its return is lost (ok=False)
    "hang_close": bool,                # closing the browser never returns
}


def validate_faults(raw, *, limits=None) -> dict:
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
                               or not driver["target_id"]
                               or not (driver["until"] == "deadline" or _seconds(driver["until"]))):
        raise ValidationError("hold_driver must be {target_id, until}")
    if raw.get("corrupt_budget_after_cancel") and "cancel_after_commit" not in raw:
        raise ValidationError("corrupt_budget_after_cancel requires cancel_after_commit")
    if limits is not None:
        if driver is not None and driver["until"] == "deadline" and limits.wall_seconds is None:
            raise ValidationError("hold_driver until deadline requires a wall deadline")
        if (raw.get("corrupt_budget_after_cancel") and limits.max_steps is None and
                limits.max_model_calls is None):
            raise ValidationError("corrupt_budget_after_cancel requires a finite counter limit")
    return deepcopy(raw)


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


class LostDriverReturn:
    """No real driver payload survives the explicitly requested lost-return fault."""
    ok = False
    text = "Acceptance fault: driver return withheld."
    artifacts = ()
    terminal = False

    def to_dict(self):
        return {"ok": False, "text": self.text, "artifacts": [], "terminal": False, "fields": {}}


class FaultBarriers:
    """Worker-local acceptance hooks; never replace the gate, adapter or its ownership.

    All waits are outside the shared gate lock. A hang deliberately requires the
    supervisor's normal bounded cancellation/deadline cleanup. Configuration is
    separate from reached barriers: only reached hooks publish a `fault` event.
    """

    def __init__(self, faults, gate, events, *, control_dir=None):
        self.faults = validate_faults(faults)
        self.gate, self.events = gate, events
        self.control_dir = Path(control_dir) if control_dir is not None else None
        self.decisions = 0
        self.replaced = False

    def _record(self, name, phase="reached"):
        # Closed vocabulary only. Never log provider state, DOM, slots or driver results.
        self.events.put(("fault", name, phase))

    def _cancel(self):
        if not request_cancel(self.gate):
            # A missing SIGINT handler must not let the intended race continue.
            from reflexmesh.runtime.runner import RuntimeStop

            self.gate.remaining()  # Preserve an already accepted deadline/cancel.
            raise RuntimeStop("fault_cancel_unaccepted")

    def before_decision(self):
        self.decisions += 1
        faults = self.faults
        if faults.get("cancel_on_decision") == self.decisions:
            self._record("cancel_on_decision")
            self._cancel()
        self.gate.remaining()
        if faults.get("provider_error_at") == self.decisions:
            self._record("provider_error_at")
            raise ValueError("acceptance provider protocol error")
        if faults.get("hang_provider_at") == self.decisions:
            self._record("hang_provider_at")
            sleep_forever()
        hold = faults.get("hold_decision")
        if hold and hold["at"] == self.decisions:
            self._record("hold_decision", "started")
            if self.control_dir is not None:
                # A08 waits on this barrier, not on elapsed startup time. The
                # service owns the private attempt directory; no path comes
                # from the fault payload and existing entries are not followed.
                descriptor = os.open(self.control_dir / "fault-hold-started",
                                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
            time.sleep(hold["seconds"])
            self._record("hold_decision", "released")
        # Even a late script/LLM request cannot begin after terminal acceptance.
        self.gate.remaining()

    def before_admit(self, inner, action, params):
        target = self.faults.get("replace_before_admit")
        if not target or self.replaced:
            return
        from reflexmesh.adapters.system_one.fixture_browser import FixtureBrowser
        from reflexmesh.runtime.runner import RuntimeStop

        if type(inner) is not FixtureBrowser:
            raise RuntimeStop("adapter_contract_unsupported")
        self.gate.remaining()
        if action not in ("click", "type_text") or type(params) is not dict:
            return  # The normal adapter will reject malformed proposals.
        descriptor = inner.describe(action, params)
        if descriptor.get("target_id") != target:
            return
        if not self.gate.permitted(descriptor.get("operation")):
            return  # Admission retains its ordinary denial, without changing the DOM.
        inner.replace_target_for_acceptance(target)
        self.replaced = True
        self._record("replace_before_admit")

    def before_commit(self, descriptor):
        """Called after revalidation, before the ordinary atomic commit."""
        operation, target = descriptor.get("operation"), descriptor.get("target_id")
        if self.faults.get("revoke_on_commit") == operation:
            sequence = self.gate.revoke(operation)
            if sequence:
                self.events.put(("revocation", operation, sequence))
                self._record("revoke_on_commit")
        if self.faults.get("cancel_before_commit") == target and target is not None:
            self._record("cancel_before_commit")
            self._cancel()

    def before_driver(self, descriptor):
        """A committed action stays in flight; do not grant a new dispatch here."""
        target = descriptor.get("target_id")
        if self.faults.get("cancel_after_commit") == target and target is not None:
            self._record("cancel_after_commit")
            self._cancel()
            if self.faults.get("corrupt_budget_after_cancel"):
                # Test sticky *failure* evidence, never raise a limit or grant a
                # reservation. This is only legal after the cancel was accepted.
                self.gate._acquire()
                try:
                    if self.gate.terminal.value == 1:
                        if self.gate.max_steps is not None:
                            self.gate.steps.value = self.gate.max_steps + 1
                        elif self.gate.max_calls is not None:
                            self.gate.calls.value = self.gate.max_calls + 1
                finally:
                    self.gate.lock.release()
                self._record("corrupt_budget_after_cancel")

    def after_driver(self, descriptor, result):
        """Only delay/discard a return that follows the real owned driver call."""
        target = descriptor.get("target_id")
        if target is None:
            return result
        if self.faults.get("hang_driver_on") == target:
            self._record("hang_driver_on")
            sleep_forever()
        hold = self.faults.get("hold_driver")
        if hold and hold["target_id"] == target:
            self._record("hold_driver", "started")
            delay = (max(0.0, self.gate.deadline - time.monotonic()) + 0.1
                     if hold["until"] == "deadline" else hold["until"])
            time.sleep(delay)
            self._record("hold_driver", "released")
        if self.faults.get("lose_return_on") == target:
            self._record("lose_return_on")
            return LostDriverReturn()
        return result

    def before_close(self):
        if self.faults.get("hang_close"):
            self._record("hang_close")
            sleep_forever()
