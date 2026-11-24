import unittest

import _bootstrap  # noqa: F401  (workspace-root path bootstrap)

from _oracle import lcp_length, optimal_witness_pair
from build_semantics import Step, UNSET, analyze, build_pipeline, replay
from build_semantics.model import State


def _sorted_pair(traces):
    """Normalize engine (list) / oracle (tuple) traces to a sorted pair."""
    return tuple(sorted(tuple(t) for t in traces))


def race_pipeline():
    """Two writers publish different values; a reader freezes one at start."""
    w1 = Step("w1", (), {(): {"v": 1}})
    w2 = Step("w2", (), {(): {"v": 2}})
    r = Step(
        "r",
        ("v",),
        {
            (1,): {"out": "saw1"},
            (2,): {"out": "saw2"},
            (UNSET,): {"out": "unset"},
        },
    )
    return build_pipeline([w1, w2, r], outputs_of_interest=["out"])


class WitnessOptimizationTests(unittest.TestCase):
    def test_witness_traces_are_legal_and_replay_to_different_outputs(self):
        p = race_pipeline()
        result = analyze(p)
        self.assertFalse(result.deterministic)
        self.assertIsNone(result.final_outputs)
        self.assertEqual(len(result.traces), 2)
        outcomes = [replay(p, trace) for trace in result.traces]
        for outcome in outcomes:
            self.assertEqual(outcome.statuses, ("ended", "ended", "ended"))
        self.assertNotEqual(outcomes[0].outputs, outcomes[1].outputs)
        self.assertEqual(set(result.possible_outputs),
                         {("saw1",), ("saw2",), ("unset",)})
        self.assertEqual(tuple(sorted(result.witness_outputs)),
                         tuple(sorted((outcomes[0].outputs, outcomes[1].outputs))))

    def test_witness_pair_matches_independent_brute_force_oracle(self):
        p = race_pipeline()
        result = analyze(p)
        t1, t2, expected_lcp = optimal_witness_pair(p, State.initial(p))
        self.assertEqual(result.shared_prefix_length, expected_lcp)
        self.assertEqual(lcp_length(*result.traces), expected_lcp)
        self.assertEqual(_sorted_pair(result.traces), tuple(sorted((t1, t2))))
        # In this graph the deepest split is reached after three shared
        # events: start(w1), end(w1), start(w2); the reader either waits for
        # the new value or freezes the stale one while w2 is still running.
        self.assertEqual(expected_lcp, 3)
        self.assertEqual(
            result.traces[0][:3], ["start(w1)", "end(w1)", "start(w2)"]
        )

    def test_deepest_split_prefers_long_chain_prefix_over_root_split(self):
        # A deterministic 3-step chain runs alongside a single race pair.
        # The witnesses must share the whole chain prefix instead of
        # diverging at the first enabled event.  The brute-force oracle
        # enumerates traces here, so the chain is kept short; the engine's
        # state-merging scale is tested separately without an oracle.
        chain = []
        table = {(i,): {"c": i + 1} for i in range(5)}
        for i in range(3):
            chain.append(
                Step(f"c{i}", ("c",), table, deps=() if i == 0 else (f"c{i - 1}",))
            )
        wa = Step("wa", (), {(): {"v": "a"}})
        wb = Step("wb", (), {(): {"v": "b"}})
        rd = Step(
            "rd",
            ("v",),
            {("a",): {"out": 1}, ("b",): {"out": 2}, (UNSET,): {"out": 0}},
        )
        p = build_pipeline(chain + [wa, wb, rd],
                           initial_values={"c": 0},
                           outputs_of_interest=["out"])
        result = analyze(p)
        self.assertFalse(result.deterministic)
        t1, t2, expected_lcp = optimal_witness_pair(p, State.initial(p))
        self.assertEqual(result.shared_prefix_length, expected_lcp)
        self.assertEqual(_sorted_pair(result.traces), tuple(sorted((t1, t2))))
        # The full chain (6 events) is shared before the race pair diverges.
        self.assertGreaterEqual(expected_lcp, 6)
        shared = result.traces[0][:expected_lcp]
        # Every chain end event sits inside the shared prefix: no chain
        # reordering is forced onto the witness traces.
        chain_ends = [e for e in shared if e.startswith("end(c")]
        self.assertEqual(chain_ends, ["end(c0)", "end(c1)", "end(c2)"])

    def test_sorted_pair_lexicographic_tie_break(self):
        # Two symmetric writers; several pairs share the same LCP.  The
        # engine must return the lexicographically smallest sorted pair.
        wa = Step("wa", (), {(): {"v": 1}})
        wb = Step("wb", (), {(): {"v": 2}})
        rd = Step("rd", ("v",),
                  {(1,): {"out": "one"}, (2,): {"out": "two"},
                   (UNSET,): {"out": "none"}})
        p = build_pipeline([wa, wb, rd], outputs_of_interest=["out"])
        result = analyze(p)
        t1, t2, expected_lcp = optimal_witness_pair(p, State.initial(p))
        self.assertEqual(_sorted_pair(result.traces), tuple(sorted((t1, t2))))
        self.assertEqual(result.shared_prefix_length, expected_lcp)
        # 'end(...)' < 'start(...)' lexicographically, so at the divergence
        # state the first trace starts with an end event.
        divergence_index = expected_lcp
        self.assertTrue(
            result.traces[0][divergence_index].startswith("end(")
            or result.traces[1][divergence_index].startswith("end(")
        )

    def test_oracle_agrees_on_random_small_graphs(self):
        import random

        from build_semantics import LookupTableError

        rng = random.Random(20261124)
        checked = 0
        for trial in range(30):
            # Sparse DAGs keep the brute-force oracle small (the engine has
            # its own state-merging and is exercised at scale separately).
            n = rng.randint(2, 4)
            steps = []
            for i in range(n):
                # Sparse, always-valid dependency edges.
                deps = tuple(f"s{i - 1}" for _ in range(1)) if i and rng.random() < 0.5 else ()
                # Total table over every frozen value the step can observe:
                # v cycles through 0..2 and UNSET before any publish.
                table = {(combo,): {"v": (i + combo) % 3, f"out{i}": combo}
                         for combo in range(3)}
                table[(UNSET,)] = {"v": i % 3, f"out{i}": -1}
                steps.append(Step(f"s{i}", ("v",), table, deps=deps))
            p = build_pipeline(steps,
                               initial_values={"v": rng.randrange(2)},
                               outputs_of_interest=("v",))
            try:
                result = analyze(p)
            except LookupTableError:
                continue
            if result.deterministic:
                continue
            t1, t2, expected_lcp = optimal_witness_pair(p, State.initial(p))
            self.assertEqual(result.shared_prefix_length, expected_lcp,
                             f"trial {trial} LCP mismatch")
            self.assertEqual(_sorted_pair(result.traces),
                             tuple(sorted((t1, t2))),
                             f"trial {trial} trace mismatch")
            checked += 1
        self.assertGreaterEqual(checked, 10)


if __name__ == "__main__":
    unittest.main()
