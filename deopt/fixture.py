"""Program construction and the two-layer inlining fixture.

Fixture story (``outer -> middle -> inner``, two inlined call layers):

* one logical *box* object is referenced multiple times: three locals in
  ``middle`` (``b1``/``b2``/``b3``), the inlined ``inner`` parameter
  (``box``) and the field ``node.back``;
* one *node* object is likewise referenced from several locals and closes a
  cycle back to the box (``box.next -> node``, ``node.back -> box``);
* the optimized trace allocates both objects virtually, so recovery must
  rebuild them without re-running their constructors;
* two failure shapes share the same execution prefix:
  - ``guard``: a normal :class:`~deopt.ir.GuardType` fails in ``middle``
    *after* the inlined inner call returned, so deoptimization restores the
    two live frames outer/middle at the normal continuation;
  - ``exception``: a guard inside ``inner`` raises, exits via the exception
    edge, and lands at ``outer``'s handler (the two inlined frames pop).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ir import (
    BindExc,
    Call,
    Const,
    EmitConst,
    Function,
    GetField,
    GuardType,
    Handler,
    Move,
    New,
    Return,
    SetField,
)
from .recovery import EXCEPTION, NORMAL, DeoptFrame, GuardFailure
from .sources import ConstSource, ReconstructSource
from .trace import (
    GUARD_FAILED,
    RAISED,
    TConst,
    TConstException,
    TEmitConst,
    TGuardTag,
    TMove,
    TNew,
    TRaise,
    TSetField,
    run_trace,
)

# --------------------------------------------------------------------------- #
# Generic IR program
# --------------------------------------------------------------------------- #


class ProgramBuilder:
    """Accumulates instructions and resolves named labels to program counters."""

    def __init__(self) -> None:
        self.body: list[Any] = []
        self.labels: dict[str, int] = {}

    def label(self, name: str) -> None:
        self.labels[name] = len(self.body)

    def emit(self, instr: Any) -> int:
        self.body.append(instr)
        return len(self.body) - 1

    def target(self, name: str) -> int:
        return self.labels[name]


def build_functions(inner_guard_expect_box: bool) -> dict[str, Function]:
    """Build outer/middle/inner.

    ``inner_guard_expect_box`` selects the exception fixture: when true the
    inner guard expects ``Box`` but receives a ``Node`` and raises.
    """

    inner_expect = "Box" if inner_guard_expect_box else "Node"

    # ---- inner(box, n2) --------------------------------------------------
    ib = ProgramBuilder()
    ib.emit(EmitConst("inner:enter"))                       # 0
    ib.emit(GetField("n_back", "n2", "back"))               # 1
    ib.emit(SetField("box", "seen", "n_back"))              # 2
    ib.emit(Const("one", 1))                                # 3
    ib.emit(SetField("box", "n", "one"))                    # 4
    ib.emit(GuardType("n2", inner_expect))                  # 5
    ib.emit(Move("result", "n2"))                           # 6
    ib.emit(Return("result"))                               # 7
    inner = Function("inner", ["box", "n2"], ib.body)

    # ---- middle(x) -------------------------------------------------------
    mb = ProgramBuilder()
    mb.emit(New("b1", "Box", "ctor:Box"))                   # 0
    mb.emit(Move("b2", "b1"))                               # 1 alias
    mb.emit(New("n1", "Node", "ctor:Node"))                 # 2
    mb.emit(Move("b3", "b1"))                               # 3 alias
    mb.emit(SetField("b2", "next", "n1"))                   # 4 box.next
    mb.emit(Move("n2", "n1"))                               # 5 alias node
    mb.emit(SetField("n2", "back", "b3"))                   # 6 node.back
    # Exception edge lands at middle_fail (the same pc the call's unwinding
    # would reach); middle itself has no handler, so it propagates to outer.
    mb.emit(Call("inner", ["b1", "n2"], "ret", fail_pc=9))  # 7
    mb.emit(GuardType("b2", "Box"))                         # 8
    mb.label("middle_fail")
    mb.emit(Return("b3"))                                   # 9
    if not inner_guard_expect_box:
        # Guard fixture: b2 is a Box but the guard expects a Node.
        mb.body[8] = GuardType("b2", "Node")
    # middle deliberately has no landing pad: a guard that fails here pops the
    # whole activation and resumes at outer's call edge (fail_pc -> handler).
    middle = Function("middle", ["x"], mb.body)

    # ---- outer(x) --------------------------------------------------------
    ob = ProgramBuilder()
    # The middle call's exception edge lands at the handler pad (pc 2); this
    # covers both an exception raised inside the inlined inner *and* a normal
    # guard that genuinely fails once generic interpretation resumes.
    call_pc = ob.emit(Call("middle", ["x"], "out", fail_pc=2))  # 0
    ob.emit(EmitConst("outer:normal-return"))                   # 1
    ob.label("outer_handler")
    ob.emit(BindExc("err"))                                     # 2
    ob.emit(EmitConst("outer:exception-handled"))               # 3
    ob.emit(Return("err"))                                      # 4
    outer = Function(
        "outer", ["x"], ob.body,
        # The exception edge lands at fail_pc == the BindExc pad itself, so
        # the table covers the call (0) through its landing statement (2).
        handlers=[Handler(start=call_pc, end=ob.target("outer_handler") + 1,
                          target=ob.target("outer_handler"))],
    )

    return {"outer": outer, "middle": middle, "inner": inner}


def positions(functions: dict[str, Function]) -> dict[str, int]:
    return {
        "outer_after_call_pc": 1,   # outer resumes just after the middle call
        "middle_guard_pc": 8,       # middle parked at its guard
        "outer_handler_pc": 2,      # BindExc landing pad
        "middle_fail_pc": 9,
        "inner_guard_pc": 5,
        "inner_return_pc": 7,
    }


# --------------------------------------------------------------------------- #
# Optimized inlined trace (shared prefix for both fixtures)
# --------------------------------------------------------------------------- #


def build_trace_ops(raise_in_inner: bool) -> list[Any]:
    ops: list[Any] = [
        # --- middle prologue (pc 0..6): virtual box + node ---------------
        TNew("b1", "vbox", "Box", "ctor:Box"),
        TMove("b2", "b1"),
        TNew("n1", "vnode", "Node", "ctor:Node"),
        TMove("b3", "b1"),
        TSetField("b2", "next", "n1"),            # middle pc 4
        TMove("n2", "n1"),                        # middle pc 5
        TSetField("n2", "back", "b3"),            # middle pc 6
        # --- inlined inner(box=b1, n2=n1), mirrors inner pc 0..4 ---------
        TEmitConst("inner:enter"),                # inner pc 0
        # inner pc 1..2: n_back = n2.back (folded to b1 via the cycle),
        # then box.seen = n_back -- but box is n2.back, so the final value
        # of seen equals box itself; the intermediate store is coalesced.
        TMove("n_back", "b1"),
        TSetField("b1", "seen", "n_back"),
        TConst("one", 1),                         # inner pc 3
        TSetField("b1", "n", "one"),              # inner pc 4
        # Inner guard: in the exception fixture its failure is lowered to an
        # exception-edge exit rather than a normal deoptimization.
        TGuardTag("n2", "Box" if raise_in_inner else "Node",
                  on_fail="raise" if raise_in_inner else "deopt"),
    ]
    if raise_in_inner:
        # The failing guard is converted into an exception value; the exit
        # follows the exception edge (not the normal continuation).
        ops += [
            TConstException("e1", "guard-failed:Box"),
            TRaise("e1"),
        ]
    else:
        # inner returned the node; back in middle the box guard fails.
        ops += [
            TMove("ret", "n2"),
            TGuardTag("b2", "Node"),   # b2 is a Box -> normal guard failure
        ]
    return ops


# --------------------------------------------------------------------------- #
# Deoptimization metadata
# --------------------------------------------------------------------------- #


@dataclass
class Fixture:
    kind: str
    functions: dict[str, Function]
    trace_result: Any
    failure: GuardFailure
    reference_pause: tuple[str, int]


def _ref(trace: Any, vobj: str) -> ReconstructSource:
    entry = trace.reconstruct[vobj]
    # Independent descriptor, same vobj -> same rebuilt object (alias kept).
    return ReconstructSource(vobj, entry.tag, dict(entry.fields))


def build_fixture(kind: str) -> Fixture:
    if kind == NORMAL:
        functions = build_functions(inner_guard_expect_box=False)
        trace = run_trace(build_trace_ops(raise_in_inner=False))
        assert trace.outcome == GUARD_FAILED
        return _guard_fixture(functions, trace)
    if kind == EXCEPTION:
        functions = build_functions(inner_guard_expect_box=True)
        trace = run_trace(build_trace_ops(raise_in_inner=True))
        assert trace.outcome == RAISED
        return _exception_fixture(functions, trace)
    raise ValueError(f"unknown fixture kind: {kind!r}")


def _guard_fixture(functions: dict[str, Function], trace: Any) -> Fixture:
    pos = positions(functions)

    # The inlined inner call already returned in the optimized path: only the
    # two outer activations (outer, middle) are live.  Every box/node local
    # gets its own descriptor but all share vobj identities.
    middle_locals = {
        "x": ConstSource("arg-marker"),
        "b1": _ref(trace, "vbox"),
        "b2": _ref(trace, "vbox"),
        "b3": _ref(trace, "vbox"),
        "n1": _ref(trace, "vnode"),
        "n2": _ref(trace, "vnode"),
        "ret": _ref(trace, "vnode"),
    }
    outer_locals = {"x": ConstSource("arg-marker")}

    frames = [
        DeoptFrame(
            function=functions["outer"],
            resume_pc=pos["outer_after_call_pc"],
            locals=outer_locals,
            exc_target=pos["outer_handler_pc"],
        ),
        DeoptFrame(
            function=functions["middle"],
            resume_pc=pos["middle_guard_pc"],
            locals=middle_locals,
            exc_target=None,  # middle has no handler: unwind to outer
            pending_stores=list(trace.pending),
        ),
    ]
    failure = GuardFailure(
        kind=NORMAL, frames=frames,
        registers=dict(trace.registers),
    )
    return Fixture(
        kind=NORMAL, functions=functions, trace_result=trace,
        failure=failure,
        reference_pause=("middle", pos["middle_guard_pc"]),
    )


def _exception_fixture(functions: dict[str, Function], trace: Any) -> Fixture:
    pos = positions(functions)

    # Here the failure happens *inside* the still-live inlined inner: all
    # three activations are described, but the exception edge pops inner and
    # middle (neither has a landing pad) and resumes at outer's handler.
    middle_locals = {
        "x": ConstSource("arg-marker"),
        "b1": _ref(trace, "vbox"),
        "b2": _ref(trace, "vbox"),
        "b3": _ref(trace, "vbox"),
        "n1": _ref(trace, "vnode"),
        "n2": _ref(trace, "vnode"),
    }
    inner_locals = {
        "box": _ref(trace, "vbox"),
        "n2": _ref(trace, "vnode"),
        "one": ConstSource(1),
    }
    outer_locals = {"x": ConstSource("arg-marker")}
    # On the exception path the call never returns, so outer's call-result
    # slot ``out`` is not live and must not appear in recovered state -- the
    # generic interpreter likewise leaves it unassigned at the landing pad.

    frames = [
        DeoptFrame(
            function=functions["outer"],
            resume_pc=pos["outer_after_call_pc"],
            locals=outer_locals,
            exc_target=pos["outer_handler_pc"],
        ),
        DeoptFrame(
            function=functions["middle"],
            resume_pc=pos["middle_fail_pc"],
            locals=middle_locals,
            exc_target=None,
            pending_stores=list(trace.pending),
        ),
        DeoptFrame(
            function=functions["inner"],
            resume_pc=pos["inner_guard_pc"],
            locals=inner_locals,
            exc_target=None,
        ),
    ]
    failure = GuardFailure(
        kind=EXCEPTION, frames=frames,
        registers=dict(trace.registers),
        exception_source=ConstSource(trace.exception_marker),
    )
    return Fixture(
        kind=EXCEPTION, functions=functions, trace_result=trace,
        failure=failure,
        reference_pause=("outer", pos["outer_handler_pc"]),
    )
