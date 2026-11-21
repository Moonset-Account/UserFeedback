import itertools
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flowupdate import (
    ModelError,
    NoScheduleError,
    check_round,
    solve,
    verify,
    verify_schedule,
)
from flowupdate.model import EGRESS, unreachable_nodes, validate_table


def valid(table):
    return not unreachable_nodes(table)


def all_valid_tables(nodes):
    for combo in itertools.product(nodes + [EGRESS], repeat=len(nodes)):
        table = dict(zip(nodes, combo))
        if valid(table):
            yield table


class ChainReversalTest(unittest.TestCase):
    def test_nine_node_reversal_needs_nine_singleton_rounds(self):
        nodes = [f"n{i}" for i in range(1, 10)]
        old = {nodes[i]: (nodes[i + 1] if i + 1 < len(nodes) else EGRESS)
               for i in range(len(nodes))}
        new = {nodes[i]: (EGRESS if i == 0 else nodes[i - 1])
               for i in range(len(nodes))}
        schedule = solve(old, new)
        self.assertEqual(schedule.round_count, 9)
        self.assertTrue(all(len(r) == 1 for r in schedule.rounds))
        self.assertTrue(verify(old, new, schedule).ok)


class MultiNodeRoundTest(unittest.TestCase):
    OLD = {"A": "B", "B": "C", "C": "D", "D": "OUT"}
    NEW = {"A": "C", "B": "A", "C": "OUT", "D": "A"}

    def test_two_rounds_with_pairs(self):
        schedule = solve(self.OLD, self.NEW)
        self.assertEqual(schedule.round_count, 2)
        self.assertEqual(sorted(len(r) for r in schedule.rounds), [2, 2])
        self.assertTrue(verify(self.OLD, self.NEW, schedule).ok)

    def test_multi_node_round_beats_singletons(self):
        # Every singleton ordering needs 3+ rounds; the pair plan needs 2.
        self.assertEqual(solve(self.OLD, self.NEW).round_count, 2)
        self.assertEqual(
            solve(self.OLD, self.NEW, max_round_size=1).round_count, 4
        )

    def test_each_pair_round_is_safe_under_every_order(self):
        # The second pair is only safe once the first pair has been applied,
        # so check each round against the table it actually runs on.
        from flowupdate import table_for_state

        first, second = (["A", "C"], ["B", "D"])
        self.assertTrue(check_round(self.OLD, first, self.NEW).safe)
        base_after_first = table_for_state(self.OLD, self.NEW, first)
        self.assertTrue(check_round(base_after_first, second, self.NEW).safe)

    def test_whole_change_set_in_one_round_is_rejected(self):
        verdict = check_round(self.OLD, list(self.NEW), self.NEW)
        self.assertFalse(verdict.safe)


class MinimumRoundsOptimalityTest(unittest.TestCase):
    """Brute-force the optimum by trying every partition into rounds."""

    def _optimal_round_count(self, old, new, max_round_size):
        changed = sorted(
            n for n in set(old) | set(new) if old.get(n) != new.get(n)
        )
        if not changed:
            return 0
        # Precompute safety for every state.
        states = {}
        n = len(changed)
        for mask in range(1 << n):
            applied = {changed[i] for i in range(n) if mask & (1 << i)}
            table = dict(old)
            for node in applied:
                table[node] = new[node]
            states[frozenset(applied)] = valid(table)

        from functools import lru_cache

        full = frozenset(changed)

        @lru_cache(maxsize=None)
        def best(state):
            if state == full:
                return 0
            remaining = [c for c in changed if c not in state]
            result = None
            for size in range(1, min(max_round_size, len(remaining)) + 1):
                for combo in itertools.combinations(remaining, size):
                    ok = all(
                        states[state | frozenset(sub)]
                        for sub in _powerset(combo)
                    )
                    if not ok:
                        continue
                    sub_best = best(state | frozenset(combo))
                    if sub_best is None:
                        continue
                    total = 1 + sub_best
                    if result is None or total < result:
                        result = total
            return result

        return best(frozenset())

    def test_solver_matches_bruteforce_on_all_four_node_pairs(self):
        nodes = ["A", "B", "C", "D"]
        tables = list(all_valid_tables(nodes))
        checked = 0
        for old in tables:
            for new in tables:
                if old == new:
                    continue
                expected = self._optimal_round_count(old, new, 9)
                try:
                    schedule = solve(old, new)
                    got = schedule.round_count
                except NoScheduleError:
                    got = None
                self.assertEqual(got, expected, msg=f"old={old} new={new}")
                checked += 1
        self.assertGreater(checked, 1000)

    def test_solver_plans_verify_on_all_four_node_pairs(self):
        nodes = ["A", "B", "C", "D"]
        tables = list(all_valid_tables(nodes))
        for old in tables:
            for new in tables:
                if old == new:
                    continue
                schedule = solve(old, new)
                result = verify(old, new, schedule)
                self.assertTrue(result.ok, msg=f"old={old} new={new}: {result.describe()}")


def _powerset(nodes):
    ordered = tuple(sorted(nodes))
    for mask in range(1 << len(ordered)):
        yield frozenset(
            ordered[i] for i in range(len(ordered)) if mask & (1 << i)
        )


class InterleavingSafetyTest(unittest.TestCase):
    def test_single_round_for_swap_is_rejected(self):
        old = {"A": "B", "B": "OUT"}
        new = {"A": "OUT", "B": "A"}
        verdict = check_round(old, ["A", "B"], new)
        self.assertFalse(verdict.safe)
        self.assertEqual(verdict.failing_subset, ("B",))

    def test_reverse_order_round_is_rejected(self):
        old = {"A": "B", "B": "OUT"}
        new = {"A": "OUT", "B": "A"}
        result = verify_schedule(old, new, [["B"], ["A"]])
        self.assertFalse(result.ok)
        self.assertIn("round 1 is unsafe", result.error)

    def test_swap_needs_two_rounds(self):
        old = {"A": "B", "B": "OUT"}
        new = {"A": "OUT", "B": "A"}
        schedule = solve(old, new)
        self.assertEqual(schedule.round_count, 2)
        self.assertTrue(verify(old, new, schedule).ok)


class BoundaryTest(unittest.TestCase):
    def test_identical_tables_yield_no_rounds(self):
        old = {"A": "OUT", "B": "A"}
        schedule = solve(old, dict(old))
        self.assertEqual(schedule.round_count, 0)
        self.assertEqual(schedule.rounds, ())
        self.assertTrue(verify(old, dict(old), schedule).ok)

    def test_more_than_nine_changes_rejected(self):
        nodes = [f"x{i}" for i in range(1, 11)]
        old = {nodes[i]: (nodes[i + 1] if i + 1 < len(nodes) else EGRESS)
               for i in range(len(nodes))}
        new = {nodes[i]: (EGRESS if i == 0 else nodes[i - 1])
               for i in range(len(nodes))}
        with self.assertRaises(ModelError):
            solve(old, new)

    def test_exactly_nine_changes_allowed(self):
        nodes = [f"x{i}" for i in range(1, 10)]
        old = {nodes[i]: (nodes[i + 1] if i + 1 < len(nodes) else EGRESS)
               for i in range(len(nodes))}
        new = {nodes[i]: (EGRESS if i == 0 else nodes[i - 1])
               for i in range(len(nodes))}
        schedule = solve(old, new)
        self.assertEqual(len(schedule.changed), 9)
        self.assertTrue(verify(old, new, schedule).ok)

    def test_missing_node_between_tables_rejected(self):
        with self.assertRaises(ModelError):
            solve({"A": "OUT"}, {"A": "OUT", "B": "A"})

    def test_unrouted_node_rejected(self):
        with self.assertRaises(ModelError):
            solve({"A": "OUT"}, {"A": None})

    def test_old_table_with_loop_rejected(self):
        with self.assertRaises(ModelError):
            solve({"A": "B", "B": "A"}, {"A": "OUT", "B": "OUT"})

    def test_unknown_next_hop_rejected(self):
        with self.assertRaises(ModelError):
            solve({"A": "OUT"}, {"A": "Z"})

    def test_egress_cannot_be_a_node(self):
        with self.assertRaises(ModelError):
            validate_table({"OUT": "OUT", "A": "OUT"})

    def test_empty_tables(self):
        schedule = solve({}, {})
        self.assertEqual(schedule.round_count, 0)
        self.assertTrue(verify({}, {}, schedule).ok)


class RoundSizeLimitTest(unittest.TestCase):
    OLD = {"A": "B", "B": "C", "C": "D", "D": "OUT"}
    NEW = {"A": "C", "B": "A", "C": "OUT", "D": "A"}

    def test_max_round_size_one_forces_more_rounds(self):
        schedule = solve(self.OLD, self.NEW, max_round_size=1)
        self.assertEqual(schedule.round_count, len(schedule.changed))
        self.assertTrue(verify(self.OLD, self.NEW, schedule).ok)

    def test_max_round_size_is_respected(self):
        schedule = solve(self.OLD, self.NEW, max_round_size=2)
        self.assertTrue(all(len(r) <= 2 for r in schedule.rounds))
        self.assertTrue(verify(self.OLD, self.NEW, schedule).ok)


class VerifierNegativeTest(unittest.TestCase):
    def test_plan_missing_a_changed_node_fails(self):
        old = {"A": "B", "B": "OUT"}
        new = {"A": "OUT", "B": "A"}
        result = verify_schedule(old, new, [["A"]])
        self.assertFalse(result.ok)
        self.assertIn("never updated", result.error)

    def test_plan_with_unrelated_node_fails(self):
        old = {"A": "B", "B": "OUT"}
        new = {"A": "OUT", "B": "A"}
        result = verify_schedule(old, new, [["A"], ["B"], ["C"]])
        self.assertFalse(result.ok)

    def test_duplicate_node_fails(self):
        old = {"A": "B", "B": "OUT"}
        new = {"A": "OUT", "B": "A"}
        result = verify_schedule(old, new, [["A"], ["A"], ["B"]])
        self.assertFalse(result.ok)

    def test_empty_round_fails(self):
        old = {"A": "B", "B": "OUT"}
        new = {"A": "OUT", "B": "A"}
        result = verify_schedule(old, new, [["A"], [], ["B"]])
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
