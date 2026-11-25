import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btree_separator import (  # noqa: E402
    BTree,
    BTreeFinder,
    ColumnSpec,
    KeySchema,
    Leaf,
    choose_separator,
    find_leaf,
)


class FinderScenarioTests(unittest.TestCase):
    def assert_all_keys_land_in_own_leaf(self, tree, leaves):
        for leaf_index, leaf in enumerate(leaves):
            for key in leaf.keys:
                result = tree.lookup_leaf(key)
                self.assertEqual(
                    result.leaf_index,
                    leaf_index,
                    msg=f"key {key} landed in {result.leaf_index}",
                )
                self.assertTrue(result.found)
                self.assertEqual(result.matching_key, key)

    def test_descending_columns_lookup(self):
        schema = KeySchema([
            ColumnSpec("a", "int", descending=True),
            ColumnSpec("t", "text", descending=True, nulls="last"),
        ])
        leaves = [
            Leaf([(30, "z", 1), (20, "y", 2)], "d0"),
            Leaf([(10, "m", 3), (5, "b", 4)], "d1"),
            Leaf([(0, "a", 5), (-5, "", 6)], "d2"),
        ]
        tree = BTree(leaves, schema, fanout=2)
        self.assertGreaterEqual(tree.root.level, 1)
        self.assert_all_keys_land_in_own_leaf(tree, leaves)

        # Boundary extremes have the routing sides required by L < S <= R.
        self.assertEqual(tree.lookup_leaf(leaves[0].maximum).leaf_index, 0)
        self.assertEqual(tree.lookup_leaf(leaves[1].minimum).leaf_index, 1)
        self.assertEqual(tree.lookup_leaf(leaves[1].maximum).leaf_index, 1)
        self.assertEqual(tree.lookup_leaf(leaves[2].minimum).leaf_index, 2)

    def test_empty_string_lookup(self):
        schema = KeySchema([ColumnSpec("t", "text", nulls="first")])
        leaves = [
            Leaf([(None, 1)], "null"),
            Leaf([("", 2), ("a", 3)], "empty"),
            Leaf([("apple", 4), ("banana", 5)], "text"),
        ]
        tree = BTree(leaves, schema, fanout=2)
        self.assert_all_keys_land_in_own_leaf(tree, leaves)

        result = tree.lookup_leaf(("", 2))
        self.assertEqual(result.leaf_name, "empty")
        self.assertTrue(result.found)

        # Direct separator scan agrees with the tree for these exact keys.
        direct = find_leaf(leaves, ("", 2), schema)
        self.assertEqual(direct.leaf_index, 1)
        self.assertTrue(direct.found)

    def test_adjacent_leaves_differ_only_by_rowid(self):
        schema = KeySchema([
            ColumnSpec("a", "int"),
            ColumnSpec("t", "text"),
        ])
        leaves = [
            Leaf([(7, "x", 1), (7, "x", 2)], "r0"),
            Leaf([(7, "x", 3), (7, "x", 4)], "r1"),
            Leaf([(7, "x", 5), (7, "x", 6)], "r2"),
            Leaf([(7, "x", 7), (7, "x", 8)], "r3"),
        ]
        tree = BTree(leaves, schema, fanout=2)
        self.assert_all_keys_land_in_own_leaf(tree, leaves)

        for separator in tree.adjacent_separators:
            self.assertEqual(separator.kind, "full_right_key")
            # int 9 + one-byte text 2 + rowid 9.
            self.assertEqual(separator.byte_length, 20)

        result = BTreeFinder(leaves, schema, fanout=3).find_leaf((7, "x", 5))
        self.assertEqual(result.leaf_name, "r2")
        self.assertTrue(result.found)

    def test_descending_empty_string_and_rowid_only_interleaving(self):
        schema = KeySchema([
            ColumnSpec("t", "text", descending=True, nulls="last"),
        ])
        leaves = [
            Leaf([("z", 1), ("b", 2)], "desc-high"),
            Leaf([("apple", 3), ("", 4)], "desc-to-empty"),
            Leaf([("", 7), ("", 8)], "empty-rowid-tie"),
            Leaf([(None, 9)], "null-last"),
        ]
        tree = BTree(leaves, schema, fanout=2)
        self.assert_all_keys_land_in_own_leaf(tree, leaves)

        # The boundary between leaves 1 and 2 differs only by rowid, so the
        # right minimum's empty text and eight-byte rowid must both be encoded.
        rowid_boundary = tree.adjacent_separators[1]
        self.assertEqual(rowid_boundary.kind, "full_right_key")
        self.assertEqual(rowid_boundary.values, (b"", 7))
        self.assertEqual(rowid_boundary.byte_length, 10)  # text tag 1 + rowid 9

        self.assertEqual(tree.lookup_leaf(("", 4)).leaf_name, "desc-to-empty")
        self.assertEqual(tree.lookup_leaf(("", 7)).leaf_name, "empty-rowid-tie")
        self.assertEqual(tree.lookup_leaf((None, 9)).leaf_name, "null-last")

    def test_separator_itself_routes_to_right_adjacent_leaf(self):
        schema = KeySchema([ColumnSpec("a", "int"), ColumnSpec("t", "text")])
        leaves = [
            Leaf([(1, "a", 1), (2, "b", 2)], "left"),
            Leaf([(3, "c", 3), (4, "d", 4)], "right1"),
            Leaf([(5, "e", 5)], "right2"),
        ]
        tree = BTree(leaves, schema, fanout=2)
        for index, separator in enumerate(tree.adjacent_separators):
            result = tree.lookup_leaf(separator)
            self.assertEqual(result.leaf_index, index + 1)

    def test_invalid_leaf_order_is_reported(self):
        schema = KeySchema([ColumnSpec("a", "int")])
        leaves = [
            Leaf([(2, 1)], "bad-left"),
            Leaf([(1, 2)], "bad-right"),
        ]
        with self.assertRaises(Exception):
            BTree(leaves, schema)


if __name__ == "__main__":
    unittest.main()
