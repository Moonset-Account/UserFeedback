"""Unit tests for :mod:`median_window.treap`."""

import random
import unittest

from median_window.treap import OrderedMultiset


class OrderedMultisetBasics(unittest.TestCase):
    def test_empty(self):
        tree = OrderedMultiset()
        self.assertEqual(len(tree), 0)
        self.assertFalse(tree)
        self.assertIsNone(tree.median())
        with self.assertRaises(IndexError):
            tree.kth(1)

    def test_single(self):
        tree = OrderedMultiset()
        tree.add(5)
        self.assertEqual(len(tree), 1)
        self.assertTrue(tree)
        self.assertEqual(tree.kth(1), 5)
        self.assertEqual(tree.median(), 5)
        self.assertTrue(tree.discard(5))
        self.assertEqual(len(tree), 0)

    def test_median_odd_and_even(self):
        tree = OrderedMultiset()
        for v in [4, 1, 3]:
            tree.add(v)
        self.assertEqual(tree.median(), 3)
        tree.add(2)
        self.assertEqual(tree.median(), (2 + 3) / 2)

    def test_duplicates_use_multiplicity(self):
        tree = OrderedMultiset()
        for v in [7, 7, 7, 1]:
            tree.add(v)
        self.assertEqual(len(tree), 4)
        self.assertEqual(tree.kth(1), 1)
        self.assertEqual(tree.kth(2), 7)
        self.assertEqual(tree.kth(4), 7)
        self.assertEqual(tree.median(), 7)
        self.assertTrue(tree.discard(7))
        self.assertEqual(len(tree), 3)
        self.assertEqual(tree.median(), 7)

    def test_discard_missing(self):
        tree = OrderedMultiset()
        tree.add(1)
        self.assertFalse(tree.discard(2))
        self.assertFalse(tree.discard(0))
        self.assertEqual(len(tree), 1)

    def test_negative_and_float_values(self):
        tree = OrderedMultiset()
        for v in [-3, -1.5, 0, 2.25]:
            tree.add(v)
        self.assertEqual(tree.kth(1), -3)
        self.assertEqual(tree.kth(4), 2.25)
        self.assertEqual(tree.median(), (-1.5 + 0) / 2)

    def test_clear_keeps_counters(self):
        tree = OrderedMultiset()
        tree.add(1)
        tree.add(2)
        before = (tree.insertions, tree.deletions)
        tree.clear()
        self.assertEqual(len(tree), 0)
        self.assertEqual((tree.insertions, tree.deletions), before)


class OrderedMultisetAgainstReference(unittest.TestCase):
    """Cross-check order statistics against a sorted list model."""

    def test_randomized(self):
        rng = random.Random(20261122)
        tree = OrderedMultiset(seed=1)
        model = []
        for _ in range(2000):
            if model and rng.random() < 0.45:
                value = rng.choice(model)
                self.assertTrue(tree.discard(value))
                model.remove(value)
            else:
                value = rng.randint(-50, 50)
                tree.add(value)
                model.append(value)
            model.sort()
            self.assertEqual(len(tree), len(model))
            if not model:
                self.assertIsNone(tree.median())
                continue
            for k in {1, len(model), len(model) // 2 + 1}:
                self.assertEqual(tree.kth(k), model[k - 1])
            n = len(model)
            expected = (
                model[n // 2]
                if n % 2
                else (model[n // 2 - 1] + model[n // 2]) / 2
            )
            self.assertEqual(tree.median(), expected)

    def test_churn_is_path_bounded(self):
        """One insert or delete must not touch the whole window."""
        rng = random.Random(7)
        n = 512
        tree = OrderedMultiset(seed=3)
        values = list(range(n))
        rng.shuffle(values)
        insertions_at_start = tree.insertions
        for v in values:
            tree.add(v)
        self.assertEqual(tree.insertions - insertions_at_start, n)
        # Rotations are O(n) total for n inserts, not O(n log n) or O(n^2).
        self.assertLessEqual(tree.rotations, 4 * n)
        rotations_before = tree.rotations
        for v in values:
            self.assertTrue(tree.discard(v))
        self.assertEqual(len(tree), 0)
        self.assertLessEqual(
            tree.rotations - rotations_before, 8 * n, "deletes rebalanced too much"
        )

    def test_probes_grow_logarithmically_per_operation(self):
        """Cost per insert/delete/query is a tree path, never a whole scan."""
        import math

        results = {}
        for n in (256, 4096):
            tree = OrderedMultiset(seed=11)
            values = list(range(n))
            random.Random(n).shuffle(values)
            for v in values:
                tree.add(v)
            for v in values:
                tree.median()
            for v in values:
                tree.discard(v)
            ops = tree.insertions + tree.deletions + tree.kth_queries
            results[n] = tree.probes / ops
        # Doubling input 16x should add only a few probes per operation
        # (log2(16) = 4), certainly not scale with n.
        self.assertLess(results[4096], results[256] + 6)
        self.assertLess(results[4096], 2 * math.log2(4096) + 6)

    def test_merge_steps_bounded_after_deletes(self):
        tree = OrderedMultiset(seed=13)
        for v in range(1000):
            tree.add(v)
        for v in range(1000):
            tree.discard(v)
        # Each merge walks at most one path per deleted node.
        self.assertLessEqual(tree.merge_steps, 20 * 1000)


if __name__ == "__main__":
    unittest.main()
