import unittest

import _bootstrap  # noqa: F401  (workspace-root path bootstrap)

from build_semantics import Step, UNSET, analyze, build_pipeline, replay


def make_chain(n):
    """n steps where each reads the previous published value and adds one."""
    steps = []
    table = {(i,): {"v": i + 1} for i in range(n + 1)}
    for i in range(n):
        deps = () if i == 0 else (f"s{i - 1:02d}",)
        steps.append(Step(f"s{i:02d}", ("v",), table, deps=deps))
    return build_pipeline(steps, initial_values={"v": 0}, outputs_of_interest=["v"])


class DeterministicTests(unittest.TestCase):
    def test_single_step_publishes_atomically(self):
        step = Step("build", ("x",), {(0,): {"a": 1, "b": 2}})
        p = build_pipeline([step], initial_values={"x": 0}, outputs_of_interest=["a", "b"])
        result = analyze(p)
        self.assertTrue(result.deterministic)
        self.assertEqual(result.final_outputs, (1, 2))
        self.assertEqual(result.possible_outputs, ((1, 2),))
        outcome = replay(p, ["start(build)", "end(build)"])
        self.assertEqual(outcome.workspace, {"x": 0, "a": 1, "b": 2})
        self.assertEqual(outcome.statuses, ("ended",))
        self.assertEqual(outcome.outputs, (1, 2))

    def test_strict_chain_is_deterministic_regardless_of_depth(self):
        p = make_chain(12)
        result = analyze(p)
        self.assertTrue(result.deterministic)
        self.assertEqual(result.final_outputs, (12,))
        # A pure chain has exactly one enabled event in every state.
        self.assertEqual(result.states_explored, 25)  # 12 steps * 2 events + root
        self.assertEqual(result.transitions, 24)

    def test_independent_publishers_that_write_disjoint_names_are_deterministic(self):
        a = Step("a", (), {(): {"x": 1}})
        b = Step("b", (), {(): {"y": 2}})
        c = Step(
            "c",
            ("x", "y"),
            {
                (1, 2): {"z": 3},
                (1, UNSET): {"z": 3},
                (UNSET, 2): {"z": 3},
                (UNSET, UNSET): {"z": 3},
            },
        )
        p = build_pipeline([a, b, c], outputs_of_interest=["z"])
        result = analyze(p)
        self.assertTrue(result.deterministic)
        self.assertEqual(result.final_outputs, (3,))

    def test_unset_read_is_part_of_the_frozen_tuple(self):
        reader = Step(
            "reader",
            ("missing",),
            {(UNSET,): {"seen": "unset"}, (7,): {"seen": "seven"}},
        )
        p = build_pipeline([reader], initial_values={}, outputs_of_interest=["seen"])
        result = analyze(p)
        self.assertTrue(result.deterministic)
        self.assertEqual(result.final_outputs, ("unset",))

    def test_overwriting_same_value_keeps_final_outputs_equal(self):
        w1 = Step("w1", (), {(): {"v": 9}})
        w2 = Step("w2", (), {(): {"v": 9}})
        r = Step("r", ("v",), {(9,): {"out": "ok"}, (UNSET,): {"out": "ok"}})
        p = build_pipeline([w1, w2, r], outputs_of_interest=["out"])
        result = analyze(p)
        self.assertTrue(result.deterministic)
        self.assertEqual(result.final_outputs, ("ok",))


if __name__ == "__main__":
    unittest.main()
