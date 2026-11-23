"""Generic fallback interpreter for the IR.

This interpreter never speculates: calls are real stacked frames, guards are
real checks and every side effect is performed exactly once.  It serves two
roles:

1. It is the *target* that deoptimization restores (a stack of
   :class:`Frame` objects plus a shared :class:`Runtime`).
2. A reference interpreter run, paused at the same logical statement, is the
   ground truth that recovered states are compared against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .ir import (
    BindExc,
    Call,
    Const,
    Emit,
    EmitConst,
    Function,
    GetField,
    GuardType,
    IRException,
    IRObject,
    Jump,
    Move,
    New,
    Raise,
    Return,
    SetField,
)


@dataclass
class Frame:
    """One interpreter frame (a concrete, non-inlined activation)."""

    function: Function
    pc: int = 0
    locals: dict[str, Any] = field(default_factory=dict)
    return_dst: str | None = None
    fail_pc: int | None = None
    # Slots reserved for call results that have not returned yet.  They are
    # not live values and must not appear in frame-state comparisons.
    pending_slots: set[str] = field(default_factory=set)

    def label(self) -> str:
        return f"{self.function.name}@{self.pc}"


class Runtime:
    """Object allocator and the externally visible effect log."""

    def __init__(self) -> None:
        self._next_oid = 0
        self.effects: list[str] = []

    def allocate(self, tag: str) -> IRObject:
        # Allocation itself is not an externally observable effect; only
        # explicit Emit/EmitConst instructions are.
        obj = IRObject(tag, self._next_oid)
        self._next_oid += 1
        return obj

    def emit(self, marker: str) -> None:
        self.effects.append(marker)


class UncaughtIRException(Exception):
    """Raised when an exception unwinds past the outermost frame."""

    def __init__(self, value: Any):
        super().__init__(f"uncaught IRException: {value!r}")
        self.value = value


class Interpreter:
    """Stepping interpreter whose state can be snapshotted and resumed."""

    def __init__(self, functions: dict[str, Function], entry: str,
                 args: list[Any] | None = None) -> None:
        self.functions = functions
        self.runtime = Runtime()
        entry_fn = functions[entry]
        first = Frame(
            function=entry_fn,
            locals=dict(zip(entry_fn.params, args or [])),
        )
        self.frames: list[Frame] = [first]
        self._in_flight: Any = None

    # ------------------------------------------------------------------ #
    # Introspection helpers
    # ------------------------------------------------------------------ #

    @property
    def top(self) -> Frame:
        return self.frames[-1]

    def stack_labels(self) -> list[str]:
        return [f.label() for f in self.frames]

    def run_until(self, predicate: Callable[["Interpreter"], bool]) -> None:
        """Step until ``predicate`` is true *before* executing a statement.

        The check happens at statement boundaries, i.e. at the same logical
        positions that deoptimization resumes at.
        """

        while not predicate(self):
            self.step()

    def run_all(self) -> Any:
        """Run until the outermost frame returns; return its result."""

        while self.frames:
            self.step()
        return self._return_value

    def adopt(self, frames: list[Frame]) -> None:
        """Atomically publish a recovered frame stack.

        The interpreter only observes the new state when the full stack is
        ready; failed recovery therefore never publishes a half-built state.
        """

        self.frames = frames

    def set_in_flight(self, value: Any) -> None:
        """Publish the exception value carried by an exception-edge exit."""

        self._in_flight = value

    # ------------------------------------------------------------------ #
    # Core stepping
    # ------------------------------------------------------------------ #

    def step(self) -> None:
        frame = self.top
        instr = frame.function.body[frame.pc]
        # Advance first; jumps/calls overwrite the pc afterwards.
        frame.pc += 1
        try:
            self._dispatch(frame, instr)
        except IRException as exc:
            self._unwind(exc.value)

    def _unwind(self, value: Any) -> None:
        """Unwind an exception to the nearest handler.

        Two source shapes are handled uniformly:

        * an instruction (including a guard) raises *inside* a frame -- the
          frame's own exception table is consulted at the raising pc, then the
          frame pops and its caller's call edge (``fail_pc``) is taken;
        * a callee has already popped and the caller resumes at its
          ``fail_pc`` -- the caller's table is consulted at that pc.

        If no handler and no call edge exist, the entry frame is *left in
        place* with the exception attached: the interpreter preserves the
        faulting scene rather than publishing an empty stack.
        """

        frame = self.top
        pc = frame.pc - 1  # pc already advanced past the raising instruction

        while self.frames:
            target = frame.function.handler_for(pc)
            if target is not None:
                frame.pc = target
                self._in_flight = value
                return

            edge = frame.fail_pc
            if not self.frames[1:]:
                # Entry frame with no handler: preserve the faulting scene.
                self._in_flight = value
                raise UncaughtIRException(value)
            self.frames.pop()
            caller = self.top
            if edge is None:
                # The popped activation had no call edge: the exception
                # escapes and the surviving caller keeps its scene.
                self._in_flight = value
                raise UncaughtIRException(value)
            caller.pc = edge
            frame, pc = caller, edge
        raise UncaughtIRException(value)  # pragma: no cover

    def _dispatch(self, frame: Frame, instr: Any) -> None:
        rt = self.runtime
        loc = frame.locals

        if isinstance(instr, Const):
            loc[instr.dst] = instr.value

        elif isinstance(instr, New):
            obj = rt.allocate(instr.tag)
            loc[instr.dst] = obj
            rt.emit(instr.ctor_effect)

        elif isinstance(instr, GetField):
            loc[instr.dst] = loc[instr.obj].fields[instr.name]

        elif isinstance(instr, SetField):
            loc[instr.obj].fields[instr.name] = loc[instr.src]

        elif isinstance(instr, Move):
            loc[instr.dst] = loc[instr.src]

        elif isinstance(instr, GuardType):
            obj = loc[instr.src]
            if not isinstance(obj, IRObject) or obj.tag != instr.expected_tag:
                raise IRException(f"guard-failed:{instr.expected_tag}")

        elif isinstance(instr, Call):
            callee = self.functions[instr.func]
            if instr.dst is not None:
                frame.pending_slots.add(instr.dst)
            callee_frame = Frame(
                function=callee,
                locals=dict(zip(callee.params,
                                [loc[a] for a in instr.args])),
                return_dst=instr.dst,
                fail_pc=instr.fail_pc,
            )
            self.frames.append(callee_frame)

        elif isinstance(instr, Return):
            value = None if instr.src is None else loc[instr.src]
            self.frames.pop()
            if not self.frames:
                self._return_value = value
                return
            if frame.return_dst is not None:
                self.top.locals[frame.return_dst] = value
                self.top.pending_slots.discard(frame.return_dst)

        elif isinstance(instr, Raise):
            raise IRException(loc[instr.src])

        elif isinstance(instr, BindExc):
            loc[instr.dst] = self._in_flight
            self._in_flight = None

        elif isinstance(instr, Jump):
            frame.pc = instr.target

        elif isinstance(instr, EmitConst):
            rt.emit(instr.marker)

        elif isinstance(instr, Emit):
            value = loc[instr.src]
            marker = f"oid:{value.oid}" if isinstance(value, IRObject) \
                else f"val:{value!r}"
            rt.emit(marker)

        else:  # pragma: no cover - defensive
            raise TypeError(f"unknown instruction: {instr!r}")
