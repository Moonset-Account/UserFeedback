import time
import unittest

import _bootstrap  # noqa: F401  (workspace-root path bootstrap)

from build_semantics import Step, UNSET, analyze, build_pipeline
from build_semantics.model import State


class StateMergingScaleTests(unittest.TestCase):
    def test_independent_long_chain_does_not_force_permutation_enumeration(self):
        # A 9-step independent chain next to a 3-step race would admit
        # hundreds of thousands of complete traces (measured: 270,270 with
        # a 4-step chain).  State merging must keep the explored state count
        # polynomial-ish and finish essentially instantly.
        chain = []
        ctable = {(i,): {"c": i + 1} for i in range(11)}
        for i in range(9):
            chain.append(
                Step(f"c{i}", ("c",), ctable,
                     deps=() if i == 0 else (f"c{i - 1}",))
            )
        wa = Step("wa", (), {(): {"v": "a"}})
        wb = Step("wb", (), {(): {"v": "b"}})
        rd = Step("rd", ("v",),
                  {("a",): {"out": 1}, ("b",): {"out": 2},
                   (UNSET,): {"out": 0}})
        p = build_pipeline(chain + [wa, wb, rd],
                           initial_values={"c": 0},
                           outputs_of_interest=["out"])
        t0 = time.perf_counter()
        result = analyze(p)
        elapsed = time.perf_counter() - t0
        self.assertFalse(result.deterministic)
        # 12 steps -> at most 3^12 = 531,441 abstract status assignments;
        # actual reachable states stay a small fraction of that.
        self.assertLess(result.states_explored, 5000)
        self.assertLess(elapsed, 2.0)
        # The long chain never participates in the divergence: every chain
        # event is shared by both witnesses.
        shared = result.traces[0][:result.shared_prefix_length]
        for i in range(9):
            self.assertIn(f"end(c{i})", shared)

    def test_twelve_step_pipeline_is_accepted_and_analyzed(self):
        steps = []
        table = {(i,): {"v": i + 1} for i in range(13)}
        for i in range(12):
            steps.append(
                Step(f"s{i:02d}", ("v",), table,
                     deps=() if i == 0 else (f"s{i - 1:02d}",))
            )
        p = build_pipeline(steps, initial_values={"v": 0},
                           outputs_of_interest=["v"])
        result = analyze(p)
        self.assertTrue(result.deterministic)
        self.assertEqual(result.final_outputs, (12,))

    def test_more_than_twelve_steps_is_rejected(self):
        from build_semantics import ValidationError

        steps = [Step(f"s{i:02d}", (), {(): {"v": i}}) for i in range(13)]
        with self.assertRaises(ValidationError):
            build_pipeline(steps)

    def test_cycle_is_rejected(self):
        from build_semantics import ValidationError

        a = Step("a", (), {(): {"x": 1}}, deps=("b",))
        b = Step("b", (), {(): {"y": 1}}, deps=("a",))
        with self.assertRaises(ValidationError):
            build_pipeline([a, b])

    def test_unknown_dependency_is_rejected(self):
        from build_semantics import ValidationError

        a = Step("a", (), {(): {"x": 1}}, deps=("ghost",))
        with self.assertRaises(ValidationError):
            build_pipeline([a])

    def test_duplicate_step_names_are_rejected(self):
        from build_semantics import ValidationError

        a1 = Step("a", (), {(): {"x": 1}})
        a2 = Step("a", (), {(): {"x": 2}})
        with self.assertRaises(ValidationError):
            build_pipeline([a1, a2])

    def test_table_arity_must_match_reads(self):
        from build_semantics import ValidationError

        with self.assertRaises(ValidationError):
            Step("a", ("x", "y"), {(1,): {"z": 1}})

    def test_lookup_table_must_cover_reachable_frozen_input(self):
        from build_semantics import LookupTableError

        w = Step("w", (), {(): {"v": 1}})
        r = Step("r", ("v",), {(1,): {"out": "ok"}})  # no UNSET entry
        p = build_pipeline([w, r], outputs_of_interest=["out"])
        with self.assertRaises(LookupTableError):
            analyze(p)

    def test_full_independence_collapses_to_one_state_per_step_boundary(self):
        # Three publishers of disjoint names with no reads: interleavings
        # differ only in event order and merge aggressively.
        a = Step("a", (), {(): {"x": 1}})
        b = Step("b", (), {(): {"y": 2}})
        c = Step("c", (), {(): {"z": 3}})
        p = build_pipeline([a, b, c], outputs_of_interest=["x", "y", "z"])
        result = analyze(p)
        self.assertTrue(result.deterministic)
        # Each of the three steps is independently pending/running/ended:
        # 3^3 = 27 memoized states, versus the 6! = 720 event orderings a
        # trace-enumerating engine would walk.
        self.assertEqual(result.states_explored, 27)


if __name__ == "__main__":
    unittest.main()
