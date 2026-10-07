"""Private immutable slot snapshots and commands for one accepted execution task.

The value-bearing snapshots are deliberately not dataclasses: generic
``dataclasses.asdict`` must not turn a private value into a public artifact.
Public serialization is explicit and contains only identities. This is an
in-process boundary, not a Python sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType

from reflexmesh.contracts.execution import ExecutionTask


class _Immutable:
    __slots__ = ()

    def __setattr__(self, name, value):
        raise AttributeError("immutable slot state")

    def __delattr__(self, name):
        raise AttributeError("immutable slot state")


class ResolvedSlot(_Immutable):
    """Exact private string, captured once; never normalize or strip it."""

    __slots__ = ("reference", "_value")

    def __init__(self, reference: str, value: str):
        if type(reference) is not str or type(value) is not str:
            raise ValueError("invalid slot snapshot")
        object.__setattr__(self, "reference", reference)
        object.__setattr__(self, "_value", value)

    def __repr__(self):
        return f"ResolvedSlot(reference={self.reference!r})"

    def to_dict(self) -> dict:
        return {"slot_ref": self.reference}


class SlotRegistry(_Immutable):
    """Supervisor-owned snapshot, independent of backend/provider dictionaries."""

    __slots__ = ("task_id", "revision", "run_id", "references", "_slots")

    def __init__(self, task: ExecutionTask):
        values = {}
        for slot in task.slots:
            if slot.reference in values:
                raise ValueError("duplicate slot reference")
            values[slot.reference] = ResolvedSlot(slot.reference, slot.value)
        object.__setattr__(self, "task_id", task.task_id)
        object.__setattr__(self, "revision", task.revision)
        object.__setattr__(self, "run_id", task.run_id)
        object.__setattr__(self, "references", tuple(values))
        object.__setattr__(self, "_slots", MappingProxyType(values))

    @classmethod
    def from_task(cls, task: ExecutionTask) -> "SlotRegistry":
        return cls(task)

    def resolve(self, reference: str) -> ResolvedSlot:
        # Exact lookup also rejects old/new versions, raw text and string subclasses.
        if type(reference) is not str or reference not in self._slots:
            raise ValueError("unknown slot reference")
        return self._slots[reference]

    def __repr__(self):
        return (f"SlotRegistry(task_id={self.task_id!r}, revision={self.revision!r}, "
                f"run_id={self.run_id!r}, references={self.references!r})")

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "revision": self.revision,
                "run_id": self.run_id, "slot_refs": list(self.references)}


class PreparedTextCommand(_Immutable):
    """Private resolved command. Its reference is never looked up by the driver."""

    __slots__ = ("index", "_slot")

    def __init__(self, index: int, slot: ResolvedSlot):
        if type(index) is not int or index < 0 or type(slot) is not ResolvedSlot:
            raise ValueError("invalid prepared text command")
        object.__setattr__(self, "index", index)
        object.__setattr__(self, "_slot", slot)

    @property
    def reference(self) -> str:
        return self._slot.reference

    @property
    def _value(self) -> str:
        return self._slot._value

    def __repr__(self):
        return f"PreparedTextCommand(index={self.index!r}, reference={self.reference!r})"

    def to_dict(self) -> dict:
        return {"index": self.index, "slot_ref": self.reference}


@dataclass(frozen=True, slots=True)
class PreparedAction:
    """One admission result supplies both public descriptor and driver command."""

    action: str
    operation: str
    target_id: str
    _parameters: tuple[tuple[str, str], ...] = field(repr=False)
    destination: str | None = None
    _text: PreparedTextCommand | None = field(default=None, repr=False)

    @property
    def slot_ref(self) -> str | None:
        return None if self._text is None else self._text.reference

    def descriptor(self) -> dict:
        result = {"operation": self.operation, "target_id": self.target_id}
        if self.destination is not None:
            result["destination"] = self.destination
        if self.slot_ref is not None:
            result["slot_ref"] = self.slot_ref
        return result

    def to_dict(self) -> dict:
        return self.descriptor()
