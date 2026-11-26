"""Tests for hierarchy validation (no individual data ever involved)."""

import unittest

from hierpost import Hierarchy, HierarchyError, Node


class ModelValidationTests(unittest.TestCase):
    def test_accepts_well_formed_tree(self):
        h = Hierarchy(
            [
                Node("root", None, weight=1, noisy=10),
                Node("a", "root", weight=2, noisy=-1, leaf_index=0),
                Node("b", "root", weight=3, leaf_index=1),
            ],
            10,
        )
        self.assertEqual(h.root, "root")
        self.assertEqual(h.leaves, ["a", "b"])
        self.assertEqual(h.depth, {"root": 0, "a": 1, "b": 1})

    def test_rejects_duplicate_ids(self):
        with self.assertRaises(HierarchyError):
            Hierarchy([Node("root", None), Node("root", "root")], 1)

    def test_rejects_multiple_roots(self):
        with self.assertRaises(HierarchyError):
            Hierarchy([Node("a", None), Node("b", None)], 1)

    def test_rejects_no_nodes(self):
        with self.assertRaises(HierarchyError):
            Hierarchy([], 1)

    def test_rejects_non_integer_root_total(self):
        for bad in (1.0, "5", True, None):
            with self.subTest(root_total=bad):
                with self.assertRaises(HierarchyError):
                    Hierarchy([Node("root", None)], bad)

    def test_rejects_negative_root_total(self):
        with self.assertRaises(HierarchyError):
            Hierarchy([Node("root", None)], -1)

    def test_rejects_unknown_parent(self):
        with self.assertRaises(HierarchyError):
            Hierarchy([Node("root", None), Node("a", "ghost", weight=1)], 1)

    def test_rejects_disconnected_cycle(self):
        nodes = [
            Node("root", None),
            Node("a", "root"),
            Node("b", "c"),
            Node("c", "b"),
        ]
        with self.assertRaises(HierarchyError):
            Hierarchy(nodes, 1)

    def test_rejects_non_positive_weight(self):
        for bad in (0, -3, True, 1.5):
            with self.subTest(weight=bad):
                with self.assertRaises(HierarchyError):
                    Hierarchy(
                        [Node("root", None), Node("a", "root", weight=bad, noisy=1)],
                        1,
                    )

    def test_rejects_float_noisy(self):
        with self.assertRaises(HierarchyError):
            Hierarchy([Node("root", None), Node("a", "root", weight=1, noisy=1.5)], 1)

    def test_measured_node_requires_weight(self):
        with self.assertRaises(HierarchyError):
            Hierarchy([Node("root", None), Node("a", "root", noisy=2)], 1)

    def test_missing_node_may_still_carry_weight(self):
        h = Hierarchy(
            [Node("root", None, weight=1, noisy=2), Node("a", "root", weight=4)],
            2,
        )
        self.assertEqual(h.root, "root")

    def test_partial_leaf_index_is_rejected(self):
        nodes = [
            Node("root", None),
            Node("a", "root", leaf_index=0),
            Node("b", "root"),
        ]
        with self.assertRaises(HierarchyError):
            Hierarchy(nodes, 1)

    def test_duplicate_leaf_index_is_rejected(self):
        nodes = [
            Node("root", None),
            Node("a", "root", leaf_index=0),
            Node("b", "root", leaf_index=0),
        ]
        with self.assertRaises(HierarchyError):
            Hierarchy(nodes, 1)

    def test_leaf_index_only_allowed_on_leaves(self):
        # "a" has a child, so it is not a leaf; giving it an index is an error.
        nodes = [
            Node("root", None),
            Node("a", "root", leaf_index=5),
            Node("b", "a", leaf_index=0),
            Node("c", "a", leaf_index=1),
        ]
        with self.assertRaises(HierarchyError):
            Hierarchy(nodes, 1)

    def test_default_leaf_order_is_sorted_id(self):
        h = Hierarchy(
            [Node("root", None), Node("z", "root"), Node("a", "root")], 1
        )
        self.assertEqual(h.leaves, ["a", "z"])

    def test_from_dict(self):
        h = Hierarchy.from_dict(
            {
                "root_total": 3,
                "nodes": {
                    "root": {"parent": None, "weight": 1, "noisy": 4},
                    "a": {"parent": "root", "weight": 1, "noisy": -1, "leaf_index": 0},
                },
            }
        )
        self.assertEqual(h.root_total, 3)
        self.assertEqual(h.leaves, ["a"])

    def test_from_dict_rejects_unknown_keys(self):
        with self.assertRaises(HierarchyError):
            Hierarchy.from_dict(
                {
                    "root_total": 1,
                    "nodes": {"root": {"parent": None, "bogus": 3}},
                }
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
