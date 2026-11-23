"""Simulation of the optimized, inlined execution.

This is *not* an optimizing compiler; it is a faithful model of the state an
optimizer would hand back when a guard fails.  A trace operates on virtual
registers and virtual objects:

* :class:`TNew` creates a virtual object whose constructor side effect is
  *not* performed (the optimization eliminated the allocation);
* :class:`TSetField` is recorded as a pending store whose value provenance is
  a :class:`~deopt.sources.ReconstructSource`, instead of mutating an object;
* :class:`TGuardTag` models a normal guard and reports a normal guard
  failure;
* :class:`TRaise` models an exception-edge exit.

Trace registers hold :class:`VirtualObject` placeholders; they are never
published into interpreter state directly.  Recovery rebuilds real objects
from the :class:`~deopt.sources.ReconstructSource` descriptions collected
here, which is what makes aliasing and cycles recoverable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .materialize import PendingStore
from .sources import ReconstructSource, RegisterSource

OK = "ok"
GUARD_FAILED = "guard_failed"
RAISED = "raised"


class VirtualObject:
    """A not-yet-materialized allocation inside the optimized trace."""

    __slots__ = ("vobj", "tag")

    def __init__(self, vobj: str, tag: str) -> None:
        self.vobj = vobj
        self.tag = tag

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"VirtualObject({self.vobj}:{self.tag})"


@dataclass
class TConst:
    dst: str
    value: Any


@dataclass
class TNew:
    dst: str
    vobj: str
    tag: str
    ctor_effect: str  # deliberately never emitted


@dataclass
class TMove:
    dst: str
    src: str


@dataclass
class TSetField:
    obj: str
    name: str
    src: str


@dataclass
class TGuardTag:
    src: str
    expected_tag: str
    # "deopt": a failed guard is the normal deoptimization trigger.
    # "raise": a failed guard is lowered to an exception-edge exit, so the
    # trace continues into the following exception-materialization ops.
    on_fail: str = "deopt"


@dataclass
class TRaise:
    src: str


@dataclass
class TEmitConst:
    marker: str


@dataclass
class TConstException:
    """Materialize an exception carrying an immutable marker string."""

    dst: str
    marker: str


@dataclass
class TraceResult:
    outcome: str
    registers: dict[str, Any]
    pending: list[PendingStore]
    reconstruct: dict[str, ReconstructSource]
    effects: list[str]
    trigger_register: str | None = None
    expected_tag: str | None = None
    exception_marker: str | None = None


def _ref(entry: dict[str, Any]) -> ReconstructSource:
    """Build a fresh descriptor referencing an existing virtual object."""

    return ReconstructSource(entry["vobj"], entry["tag"],
                             dict(entry["fields"]))


def run_trace(ops: list[Any],
              initial_registers: dict[str, Any] | None = None) -> TraceResult:
    """Execute an inlined trace until it completes, a guard fails or it raises."""

    registers: dict[str, Any] = dict(initial_registers or {})
    pending: list[PendingStore] = []
    # vobj -> {"vobj", "tag", "fields": {field: ReconstructSource}}
    virtuals: dict[str, dict[str, Any]] = {}
    effects: list[str] = []

    def reg(name: str) -> Any:
        if name not in registers:
            raise KeyError(f"trace reads undefined register {name!r}")
        return registers[name]

    def source_of(name: str) -> Any:
        """Provenance of register ``name``: immutable scalar or object ref."""

        value = reg(name)
        if isinstance(value, VirtualObject):
            return _ref(virtuals[value.vobj])
        return value

    def reconstruct_sources() -> dict[str, ReconstructSource]:
        return {vobj: _ref(entry) for vobj, entry in virtuals.items()}

    for op in ops:
        if isinstance(op, TConst):
            registers[op.dst] = op.value

        elif isinstance(op, TNew):
            registers[op.dst] = VirtualObject(op.vobj, op.tag)
            virtuals[op.vobj] = {
                "vobj": op.vobj, "tag": op.tag, "fields": {},
            }
            # Intentionally no side effect: the constructor was eliminated.

        elif isinstance(op, TMove):
            registers[op.dst] = reg(op.src)

        elif isinstance(op, TSetField):
            target = reg(op.obj)
            if not isinstance(target, VirtualObject):
                raise TypeError("TSetField target is not a virtual object")
            field_source = source_of(op.src)
            if isinstance(field_source, ReconstructSource):
                stored = field_source
            else:
                # Immutable scalar field: keep it as a register-less value via
                # a fresh scalar source resolved by source_of callers.
                from .sources import ConstSource
                stored = ConstSource(field_source)
            virtuals[target.vobj]["fields"][op.name] = stored
            pending.append(
                PendingStore(vobj=target.vobj, field=op.name,
                             source=stored)
            )

        elif isinstance(op, TGuardTag):
            value = reg(op.src)
            mismatch = (not isinstance(value, VirtualObject)
                        or value.tag != op.expected_tag)
            if mismatch and op.on_fail == "deopt":
                return TraceResult(
                    GUARD_FAILED, dict(registers), pending,
                    reconstruct_sources(), effects,
                    trigger_register=op.src, expected_tag=op.expected_tag,
                )
            # on_fail == "raise": fall through; the subsequent
            # TConstException/TRaise ops model the exception-edge lowering.

        elif isinstance(op, TConstException):
            registers[op.dst] = op.marker

        elif isinstance(op, TRaise):
            marker = reg(op.src)
            return TraceResult(
                RAISED, dict(registers), pending,
                reconstruct_sources(), effects,
                trigger_register=op.src, exception_marker=marker,
            )

        elif isinstance(op, TEmitConst):
            effects.append(op.marker)

        else:  # pragma: no cover - defensive
            raise TypeError(f"unknown trace op: {op!r}")

    return TraceResult(OK, registers, pending,
                       reconstruct_sources(), effects)
