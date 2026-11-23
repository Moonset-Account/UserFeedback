"""Deoptimization entry point: rebuild a full interpreter state.

Input is a :class:`GuardFailure` describing

* the guard failure point and whether it is a *normal* guard failure or an
  *exception edge* exit (the two carry different continuation positions);
* one :class:`DeoptFrame` per inlined layer, each giving the interpreter
  statement position (resume pc / exception landing pad) and its still-live
  locals with value provenance;
* the register/constant bank feeding :mod:`deopt.materialize`;
* field stores the optimizer had performed virtually (pending stores).

Recovery builds the complete state in private storage and only publishes it
to the interpreter once every value exists -- there is no half interpreter
state observable on failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .interpreter import Frame, Interpreter
from .ir import Function
from .materialize import (
    MISSING,
    Materializer,
    PendingStore,
    RecoveryError,
)

NORMAL = "guard"
EXCEPTION = "exception"


@dataclass
class DeoptFrame:
    """One inlined layer as it must reappear as a concrete interpreter frame.

    ``resume_pc`` is the next statement for a normal guard failure.
    ``exc_target`` is the exception-edge landing pad used when this layer is
    reached via exception propagation; ``None`` means the exception must
    continue unwinding to the caller frame.
    """

    function: Function
    resume_pc: int
    locals: dict[str, Any]  # value -> Source | MISSING
    exc_target: int | None = None
    pending_stores: list[PendingStore] = field(default_factory=list)


@dataclass
class GuardFailure:
    kind: str  # NORMAL or EXCEPTION
    frames: list[DeoptFrame]  # outer-first, leaf-last
    registers: dict[str, Any]
    exception_source: Any = None  # required for EXCEPTION

    def leaf(self) -> DeoptFrame:
        return self.frames[-1]


@dataclass
class RecoveredState:
    kind: str
    frames: list[Frame]

    def labels(self) -> list[str]:
        return [f.label() for f in self.frames]


def recover(interp: Interpreter, failure: GuardFailure) -> RecoveredState:
    """Rebuild ``interp``'s state from ``failure`` and publish it atomically.

    Raises :class:`RecoveryError` (without touching the interpreter) when a
    live value lacks provenance or an exception edge has no landing pad.
    """

    if failure.kind not in (NORMAL, EXCEPTION):
        raise ValueError(f"unknown failure kind: {failure.kind!r}")
    if failure.kind == EXCEPTION and failure.exception_source is None:
        raise ValueError("exception exit requires an exception_source")
    if not failure.frames:
        raise ValueError("deoptimization requires at least one frame")

    materializer = Materializer(interp.runtime, failure.registers)

    # Phase 1: build every frame's locals and every (cyclic) object graph in
    # private storage.  Nothing is visible to the interpreter yet.
    rebuilt: list[Frame] = []
    for index, desc in enumerate(failure.frames):
        frame_name = desc.function.name
        local_values: dict[str, Any] = {}
        for local_name, source in desc.locals.items():
            if source is MISSING or source is None:
                raise RecoveryError(frame_name, local_name,
                                    "no provenance recorded")
            local_values[local_name] = materializer.materialize(
                source, frame_name, local_name,
            )
        # Re-derive the frame's exception edge: each inlined activation was
        # entered through a Call in its parent, positioned one statement
        # before the parent's resume pc.  Without this edge a guard that fails
        # *after* recovery could not unwind along the original call chain.
        fail_pc: int | None = None
        if index > 0:
            parent = failure.frames[index - 1]
            parent_body = parent.function.body
            call_index = parent.resume_pc - 1
            if 0 <= call_index < len(parent_body):
                fail_pc = getattr(parent_body[call_index], "fail_pc", None)
        rebuilt.append(
            Frame(
                function=desc.function,
                pc=desc.resume_pc,
                locals=local_values,
                fail_pc=fail_pc,
            )
        )

    # Phase 2: replay eliminated stores onto the rebuilt objects.  These are
    # field assignments only; constructor effects are never replayed.
    for desc in failure.frames:
        materializer.apply_pending(desc.pending_stores, desc.function.name)

    # Phase 3: choose continuation positions.  Normal guard failures keep all
    # frames and resume each layer at its next statement; exception exits pop
    # frames until a layer with an exception landing pad is found.
    if failure.kind == NORMAL:
        exception_value = None
    else:
        leaf_name = failure.leaf().function.name
        exception_value = materializer.materialize(
            failure.exception_source, leaf_name, "<exception>",
        )
        while rebuilt:
            desc = failure.frames[len(rebuilt) - 1]
            if desc.exc_target is not None:
                rebuilt[-1].pc = desc.exc_target
                break
            rebuilt.pop()
        if not rebuilt:
            raise RecoveryError(
                leaf_name, "<exception>",
                "exception edge has no landing pad in any inlined frame",
            )

    # Phase 4: single atomic publish.  Any error above leaves the interpreter
    # exactly as it was before recovery was attempted.
    interp.adopt(rebuilt)
    if exception_value is not None:
        interp.set_in_flight(exception_value)
    return RecoveredState(failure.kind, rebuilt)
