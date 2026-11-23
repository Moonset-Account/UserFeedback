"""Behaviour tests for the generic fallback interpreter itself."""

from __future__ import annotations

import unittest

from deopt import (
    BindExc,
    Call,
    Const,
    EmitConst,
    Function,
    GuardType,
    Handler,
    IRException,
    Interpreter,
    Move,
    New,
    Raise,
    Return,
    SetField,
    UncaughtIRException,
)


def _functions():
    raiser = Function(
        "raiser", ["v"],
        [Raise("v"), Return(None)],
    )
    # no_handler has no exception table of its own; the call's fail_pc lands
    # just after the call (pc 1), which is outside any handler range and so
    # keeps unwinding to its caller.
    no_handler = Function(
        "no_handler", ["v"],
        [Call("raiser", ["v"], None, fail_pc=1),
         Return(None)],
    )
    catcher = Function(
        "catcher", ["v"],
        [Call("no_handler", ["v"], "out", fail_pc=1),   # 0
         EmitConst("normal"),                             # 1 (skipped on raise)
         BindExc("err"),                                  # 2 handler pad
         EmitConst("handled"),                            # 3
         Return("err")],                                  # 4
        # Cover both the call site and its fail_pc landing statement.
        handlers=[Handler(start=0, end=2, target=2)],
    )
    return {"raiser": raiser, "no_handler": no_handler,
            "catcher": catcher}


class ExceptionUnwindingTest(unittest.TestCase):
    def test_exception_crosses_two_frames_to_handler(self):
        interp = Interpreter(_functions(), "catcher", ["boom"])
        result = interp.run_all()
        self.assertEqual(result, "boom")
        self.assertEqual(interp.runtime.effects, ["handled"])
        self.assertIsNone(interp._in_flight)

    def test_uncaught_exception_escapes(self):
        funcs = _functions()
        funcs["catcher"] = Function(
            "catcher", ["v"],
            [Call("no_handler", ["v"], "out", fail_pc=None),
             Return(None)],
        )
        interp = Interpreter(funcs, "catcher", ["boom"])
        with self.assertRaises(UncaughtIRException) as ctx:
            interp.run_all()
        self.assertEqual(ctx.exception.value, "boom")
        # The entry frame is preserved as the faulting scene, with the
        # exception still in flight rather than an empty stack published.
        self.assertEqual([f.function.name for f in interp.frames], ["catcher"])
        self.assertEqual(interp._in_flight, "boom")

    def test_failed_guard_raises_ir_exception(self):
        fn = Function(
            "g", [],
            [New("o", "Box", "ctor"),
             GuardType("o", "Node"),
             Return("o")],
        )
        interp = Interpreter({"g": fn}, "g")
        with self.assertRaises(UncaughtIRException) as ctx:
            interp.run_all()
        self.assertEqual(ctx.exception.value, "guard-failed:Node")

    def test_side_effects_performed_once_in_order(self):
        fn = Function(
            "f", [],
            [EmitConst("a"), EmitConst("b"), Const("x", 1),
             EmitConst("c"), Return(None)],
        )
        interp = Interpreter({"f": fn}, "f")
        interp.run_all()
        self.assertEqual(interp.runtime.effects, ["a", "b", "c"])

    def test_setfield_then_getfield_roundtrip(self):
        fn = Function(
            "f", [],
            [New("o", "Box", "ctor"), Const("v", 42),
             SetField("o", "n", "v"),
             New("p", "Box", "ctor"),
             Move("p", "o"),
             Return("p")],
        )
        interp = Interpreter({"f": fn}, "f")
        result = interp.run_all()
        self.assertEqual(result.fields["n"], 42)


class RunUntilTest(unittest.TestCase):
    def test_pause_at_statement_boundary(self):
        fn = Function(
            "f", [],
            [EmitConst("a"), EmitConst("b"), Return(None)],
        )
        interp = Interpreter({"f": fn}, "f")
        interp.run_until(lambda it: it.top.pc == 1)
        self.assertEqual(interp.runtime.effects, ["a"])
        interp.run_all()
        self.assertEqual(interp.runtime.effects, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
