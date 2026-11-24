"""Tests for Label dominance/merge and Clearance bounds."""

import unittest

from mac_access import Clearance, Label


class LabelTests(unittest.TestCase):
    def test_level_ordering(self):
        self.assertTrue(Label(2).dominates(Label(1)))
        self.assertTrue(Label(1) <= Label(2))
        self.assertFalse(Label(1).dominates(Label(2)))
        self.assertEqual(Label(3), Label(3))

    def test_categories_must_be_subset_for_dominance(self):
        low = Label(1, {"a"})
        high = Label(1, {"a", "b"})
        self.assertTrue(high.dominates(low))
        self.assertFalse(low.dominates(high))
        # Same level, disjoint categories: incomparable.
        self.assertFalse(Label(1, {"a"}).dominates(Label(1, {"b"})))

    def test_level_does_not_compensate_for_missing_category(self):
        # A higher level without the category still fails to dominate.
        self.assertFalse(Label(5, {"a"}).dominates(Label(1, {"a", "b"})))

    def test_merge_is_least_upper_bound(self):
        merged = Label(1, {"a"}).merge(Label(3, {"b"}))
        self.assertEqual(merged, Label(3, {"a", "b"}))
        a = Label(2, {"a", "b"})
        b = Label(1, {"b", "c"})
        merged = a | b
        self.assertEqual(merged.level, 2)
        self.assertEqual(merged.categories, frozenset({"a", "b", "c"}))
        self.assertTrue(merged.dominates(a))
        self.assertTrue(merged.dominates(b))

    def test_merge_idempotent(self):
        lab = Label(2, {"a"})
        self.assertEqual(lab.merge(lab), lab)

    def test_clearance_permits_bound_and_below(self):
        clearance = Clearance(Label(3, {"a", "b"}))
        self.assertTrue(clearance.permits(Label(1, {"a"})))
        self.assertTrue(clearance.permits(Label(3, {"a", "b"})))
        self.assertIn(Label(0), clearance)
        self.assertFalse(clearance.permits(Label(4)))
        self.assertFalse(clearance.permits(Label(3, {"a", "c"})))

    def test_clearance_constructor_shorthand(self):
        self.assertEqual(Clearance(2, {"x"}).bound, Label(2, {"x"}))

    def test_invalid_labels_rejected(self):
        with self.assertRaises(ValueError):
            Label(-1)
        with self.assertRaises(TypeError):
            Label(1.5)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Label(1, [1, 2])  # type: ignore[list-item]

    def test_labels_are_frozen(self):
        lab = Label(1, {"a"})
        with self.assertRaises((AttributeError, Exception)):
            lab.level = 5  # type: ignore[misc]

    def test_process_initial_label_outside_clearance_rejected(self):
        from mac_access import AccessMonitor

        monitor = AccessMonitor()
        with self.assertRaises(ValueError):
            monitor.register_process(
                "p", Label(5), Clearance(Label(2))
            )


if __name__ == "__main__":
    unittest.main()
