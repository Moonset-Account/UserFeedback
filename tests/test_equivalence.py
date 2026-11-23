"""Acceptance tests: recovered state vs. an interpreter at the same position.

Covers the two required failure shapes of the two-layer inlining fixture:

* normal guard failure (guard continuation, all live frames retained);
* exception-edge exit (exception continuation, inlined frames popped to the
  nearest landing pad).

For each shape we compare the recovered interpreter with a fresh generic
interpreter paused at the *same logical statement*, then step both forward and
compare the next visible operations, object relationships and final results.
"""

from __future__ import annotations

import unittest

from deopt import Interpreter
from deopt.fixture import build_fixture
from deopt.recovery import EXCEPTION, NORMAL, recover
from deopt.trace import run_trace  # noqa: F401 (sanity import)

from .statecompare import (
    assert_states_equivalent,
    canonical_graph,
    frame_roots,
    frame_signature,
    user_effects,
)


def _run_reference(functions, pause_fn_name, pause_pc, args=("arg-marker",)):
    interp = Interpreter(functions, "outer", list(args))
    interp.run_until(
        lambda it: (it.top.function.name == pause_fn_name
                    and it.top.pc == pause_pc)
    )
    return interp


class GuardFailureEquivalenceTest(unittest.TestCase):
    def setUp(self):
        self.fixture = build_fixture(NORMAL)

    def test_recovered_state_matches_reference_at_guard(self):
        fx = self.fixture
        recovered = Interpreter(fx.functions, "outer", ["arg-marker"])
        state = recover(recovered, fx.failure)

        # Continuation kind and frame layout.
        self.assertEqual(state.kind, NORMAL)
        pause_name, pause_pc = fx.reference_pause
        reference = _run_reference(fx.functions, pause_name, pause_pc)

        assert_states_equivalent(self, recovered, reference)
        self.assertEqual(
            state.labels(), ["outer@1", "middle@8"],
            "normal continuation retains outer+middle at the guard",
        )

    def test_same_object_referenced_twice_stays_aliased(self):
        fx = self.fixture
        recovered = Interpreter(fx.functions, "outer", ["arg-marker"])
        recover(recovered, fx.failure)

        middle = recovered.frames[-1]
        self.assertIs(middle.locals["b1"], middle.locals["b2"])
        self.assertIs(middle.locals["b2"], middle.locals["b3"])
        self.assertIs(middle.locals["n1"], middle.locals["n2"])
        self.assertIs(middle.locals["n2"], middle.locals["ret"])
        box = middle.locals["b1"]
        node = middle.locals["n1"]
        self.assertIs(box.fields["next"], node)
        self.assertIs(node.fields["back"], box)
        # The inlined inner's stores survived onto the rebuilt box: n is the
        # folded constant, seen is n2.back which the cycle resolves to box.
        self.assertEqual(box.fields["n"], 1)
        self.assertIs(box.fields["seen"], box)

    def test_next_visible_operations_match(self):
        fx = self.fixture
        recovered = Interpreter(fx.functions, "outer", ["arg-marker"])
        recover(recovered, fx.failure)
        # The optimized trace already performed these user-visible effects;
        # publish them onto the recovered runtime so post-resume increments
        # are comparable with the generic run at the same logical position.
        for effect in fx.trace_result.effects:
            recovered.runtime.emit(effect)
        pause_name, pause_pc = fx.reference_pause
        reference = _run_reference(fx.functions, pause_name, pause_pc)
        rec_before = list(recovered.runtime.effects)
        ref_before = list(reference.runtime.effects)

        # From the same logical guard position both generic interpreters
        # take the identical next steps: the guard now really fails, the
        # exception unwinds to outer's handler and is returned.
        rec_result = recovered.run_all()
        ref_result = reference.run_all()
        self.assertEqual(rec_result, ref_result)

        # Post-resume visible operation increments are identical, so no
        # already-confirmed effect is executed a second time on bail-out.
        self.assertEqual(
            user_effects(recovered.runtime.effects[len(rec_before):]),
            user_effects(reference.runtime.effects[len(ref_before):]),
        )
        # Recovery rebuilt the two optimized-away objects without running
        # their constructors: no ctor effect was emitted during recovery or
        # during the remaining generic steps (the objects already exist).
        rec_effects = recovered.runtime.effects
        self.assertEqual(rec_effects.count("ctor:Box"), 0)
        self.assertEqual(rec_effects.count("ctor:Node"), 0)
        # For comparison, a fully generic run does emit each constructor once.
        self.assertEqual(reference.runtime.effects.count("ctor:Box"), 1)
        self.assertEqual(reference.runtime.effects.count("ctor:Node"), 1)

    def test_constructor_effects_not_replayed_on_recovery(self):
        # Build an interpreter whose runtime already recorded the optimized
        # trace's user effects; recovery adds nothing observable by itself.
        fx = self.fixture
        interp = Interpreter(fx.functions, "outer", ["arg-marker"])
        before = list(interp.runtime.effects)
        recover(interp, fx.failure)
        self.assertEqual(interp.runtime.effects, before)
        # Rebuilt objects exist despite no constructor effect being emitted.
        middle = interp.frames[-1]
        self.assertEqual(middle.locals["b1"].tag, "Box")
        self.assertEqual(middle.locals["n1"].tag, "Node")


class ExceptionEdgeEquivalenceTest(unittest.TestCase):
    def setUp(self):
        self.fixture = build_fixture(EXCEPTION)

    def test_exception_continuation_pops_to_landing_pad(self):
        fx = self.fixture
        recovered = Interpreter(fx.functions, "outer", ["arg-marker"])
        state = recover(recovered, fx.failure)

        self.assertEqual(state.kind, EXCEPTION)
        pause_name, pause_pc = fx.reference_pause
        reference = _run_reference(fx.functions, pause_name, pause_pc)

        # Three frames were described; the exception edge leaves only outer,
        # parked at its BindExc landing pad with the exception in flight.
        self.assertEqual(state.labels(), ["outer@2"])
        assert_states_equivalent(self, recovered, reference)
        self.assertEqual(recovered._in_flight, "guard-failed:Box")

    def test_exception_and_guard_carry_different_continuations(self):
        guard_fx = build_fixture(NORMAL)
        exc_fx = build_fixture(EXCEPTION)
        guard_interp = Interpreter(guard_fx.functions, "outer", ["arg-marker"])
        exc_interp = Interpreter(exc_fx.functions, "outer", ["arg-marker"])

        recover(guard_interp, guard_fx.failure)
        recover(exc_interp, exc_fx.failure)

        # Same failure point lineage, different continuations.
        self.assertEqual(
            [f.label() for f in guard_interp.frames],
            ["outer@1", "middle@8"],
        )
        self.assertEqual(
            [f.label() for f in exc_interp.frames], ["outer@2"],
        )
        self.assertIsNone(guard_interp._in_flight)
        self.assertIsNotNone(exc_interp._in_flight)

    def test_next_visible_operations_match_after_exception_edge(self):
        fx = self.fixture
        recovered = Interpreter(fx.functions, "outer", ["arg-marker"])
        recover(recovered, fx.failure)
        # Carry over effects the optimized trace had already confirmed.
        for effect in fx.trace_result.effects:
            recovered.runtime.emit(effect)
        pause_name, pause_pc = fx.reference_pause
        reference = _run_reference(fx.functions, pause_name, pause_pc)
        rec_before = len(recovered.runtime.effects)
        ref_before = len(reference.runtime.effects)

        rec_result = recovered.run_all()
        ref_result = reference.run_all()
        self.assertEqual(rec_result, ref_result)
        self.assertEqual(
            user_effects(recovered.runtime.effects[rec_before:]),
            user_effects(reference.runtime.effects[ref_before:]),
        )
        # Both handled the exception and returned its marker.
        self.assertEqual(rec_result, "guard-failed:Box")
        self.assertIn("outer:exception-handled", recovered.runtime.effects)

    def test_object_graph_survives_exception_exit(self):
        # Even though inner/middle frames are popped, materialization had to
        # rebuild the full cyclic graph while they were described.
        fx = self.fixture
        recovered = Interpreter(fx.functions, "outer", ["arg-marker"])
        recover(recovered, fx.failure)
        # The graph was built during recovery even though inner/middle were
        # popped by the exception edge: outer is intact at its pad.  The call
        # never returned, so its result slot is not live.
        self.assertEqual(frame_signature(recovered),
                         [("outer", 2, ["x"])])
        self.assertEqual(recovered._in_flight, "guard-failed:Box")


class GraphCanonicalizationTest(unittest.TestCase):
    def test_canonical_graph_detects_alias_and_cycle(self):
        fx = build_fixture(NORMAL)
        recovered = Interpreter(fx.functions, "outer", ["arg-marker"])
        recover(recovered, fx.failure)
        pause_name, pause_pc = fx.reference_pause
        reference = _run_reference(fx.functions, pause_name, pause_pc)
        self.assertEqual(
            canonical_graph(frame_roots(recovered.frames)),
            canonical_graph(frame_roots(reference.frames)),
        )


if __name__ == "__main__":
    unittest.main()
