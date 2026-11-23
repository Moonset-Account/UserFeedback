"""Recovery orchestration tests: continuations, atomicity, error surfaces."""

from __future__ import annotations

import copy
import unittest

from deopt import (
    EXCEPTION,
    MISSING,
    NORMAL,
    ConstSource,
    DeoptFrame,
    GuardFailure,
    Interpreter,
    RecoveryError,
    ReconstructSource,
    recover,
)
from deopt.fixture import build_fixture
from deopt.ir import Function


def _bare_interpreter(functions):
    """An interpreter whose only frame is the entry frame pre-recovery."""

    return Interpreter(functions, "outer", ["arg-marker"])


def _fn(name: str) -> Function:
    return Function(name, [], [])


class RecoverAtomicityTest(unittest.TestCase):
    def test_interpreter_unchanged_when_recovery_fails(self):
        fx = build_fixture(NORMAL)
        interp = _bare_interpreter(fx.functions)
        before_labels = [f.label() for f in interp.frames]
        before_locals = copy.deepcopy(
            [(f.function.name, f.pc, sorted(f.locals))
             for f in interp.frames]
        )

        bad_frames = [
            DeoptFrame(
                function=d.function,
                resume_pc=d.resume_pc,
                locals=dict(d.locals, doomed=MISSING),
                exc_target=d.exc_target,
                pending_stores=d.pending_stores,
            )
            for d in fx.failure.frames
        ]
        failure = GuardFailure(
            kind=NORMAL, frames=bad_frames,
            registers=dict(fx.failure.registers),
        )
        with self.assertRaises(RecoveryError) as ctx:
            recover(interp, failure)
        self.assertEqual(ctx.exception.value, "doomed")

        # No half state: the interpreter still shows its pre-recovery frame.
        self.assertEqual([f.label() for f in interp.frames], before_labels)
        self.assertEqual(
            [(f.function.name, f.pc, sorted(f.locals))
             for f in interp.frames],
            before_locals,
        )

    def test_error_identifies_frame_and_value(self):
        fx = build_fixture(EXCEPTION)
        bad_frames = [
            DeoptFrame(
                function=d.function, resume_pc=d.resume_pc,
                locals=dict(d.locals), exc_target=d.exc_target,
                pending_stores=d.pending_stores,
            )
            for d in fx.failure.frames
        ]
        bad_frames[2].locals["box"] = MISSING  # inner frame
        failure = GuardFailure(
            kind=EXCEPTION, frames=bad_frames,
            registers=dict(fx.failure.registers),
            exception_source=ConstSource("boom"),
        )
        interp = _bare_interpreter(fx.functions)
        with self.assertRaises(RecoveryError) as ctx:
            recover(interp, failure)
        self.assertEqual(ctx.exception.frame, "inner")
        self.assertEqual(ctx.exception.value, "box")

    def test_effect_log_untouched_after_failure(self):
        fx = build_fixture(NORMAL)
        interp = _bare_interpreter(fx.functions)
        interp.runtime.emit("already:visible")
        bad_frames = [
            DeoptFrame(
                function=d.function, resume_pc=d.resume_pc,
                locals=dict(d.locals, ghost=MISSING),
                exc_target=d.exc_target, pending_stores=d.pending_stores,
            )
            for d in fx.failure.frames
        ]
        failure = GuardFailure(
            kind=NORMAL, frames=bad_frames,
            registers=dict(fx.failure.registers),
        )
        with self.assertRaises(RecoveryError):
            recover(interp, failure)
        self.assertEqual(interp.runtime.effects, ["already:visible"])


class ContinuationValidationTest(unittest.TestCase):
    def _frames(self, with_pad=True):
        f_outer, f_mid, f_inner = _fn("outer"), _fn("middle"), _fn("inner")
        common = ReconstructSource("v", "Box", {})
        outer = DeoptFrame(
            f_outer, 1, {"x": ConstSource(0)},
            exc_target=2 if with_pad else None,
        )
        middle = DeoptFrame(f_mid, 9, {"b": common}, exc_target=None)
        inner = DeoptFrame(f_inner, 5, {"box": common}, exc_target=None)
        return [outer, middle, inner]

    def test_exception_without_any_landing_pad_fails(self):
        fx = build_fixture(NORMAL)
        interp = _bare_interpreter(fx.functions)
        before = [f.label() for f in interp.frames]
        failure = GuardFailure(
            kind=EXCEPTION, frames=self._frames(with_pad=False),
            registers={}, exception_source=ConstSource("boom"),
        )
        with self.assertRaises(RecoveryError) as ctx:
            recover(interp, failure)
        self.assertIn("no landing pad", ctx.exception.reason)
        # Nothing published despite frame popping during the attempt.
        self.assertEqual([f.label() for f in interp.frames], before)

    def test_exception_requires_exception_source(self):
        fx = build_fixture(NORMAL)
        interp = _bare_interpreter(fx.functions)
        failure = GuardFailure(
            kind=EXCEPTION, frames=self._frames(), registers={},
        )
        with self.assertRaises(ValueError):
            recover(interp, failure)

    def test_unknown_kind_rejected(self):
        fx = build_fixture(NORMAL)
        interp = _bare_interpreter(fx.functions)
        failure = GuardFailure(
            kind="sideways", frames=self._frames(), registers={},
        )
        with self.assertRaises(ValueError):
            recover(interp, failure)

    def test_empty_frames_rejected(self):
        fx = build_fixture(NORMAL)
        interp = _bare_interpreter(fx.functions)
        failure = GuardFailure(kind=NORMAL, frames=[], registers={})
        with self.assertRaises(ValueError):
            recover(interp, failure)


class CrossFrameAliasTest(unittest.TestCase):
    def test_surviving_frame_published_at_landing_pad(self):
        fx = build_fixture(EXCEPTION)
        interp = _bare_interpreter(fx.functions)
        state = recover(interp, fx.failure)
        self.assertEqual(state.kind, EXCEPTION)
        # inner/middle were popped by the exception edge; outer is parked at
        # its BindExc pad with the exception in flight and no half state.
        self.assertEqual(
            [(f.function.name, f.pc, sorted(f.locals))
             for f in interp.frames],
            [("outer", 2, ["x"])],
        )
        self.assertEqual(interp._in_flight, "guard-failed:Box")


if __name__ == "__main__":
    unittest.main()
