import random
import sys
import unittest
from functools import cmp_to_key
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btree_separator import (  # noqa: E402
    BTree,
    ColumnSpec,
    KeySchema,
    Leaf,
    choose_separator,
    prefix_candidates,
    separator_sort_key,
    valid_separators,
)
from btree_separator.model import compare_keys  # noqa: E402


class InterleavingPropertyTests(unittest.TestCase):
    def test_random_composite_orders_prefixes_and_lookups(self):
        rng = random.Random(17223)
        text_values = [
            "",
            "a",
            "aa",
            "ab",
            "b",
            "apple",
            "apricot",
            "banana",
            "é",
            b"\x00",
            b"a\xff",
        ]

        for case_number in range(60):
            column_count = rng.randint(1, 3)
            columns = []
            for _ in range(column_count):
                columns.append(
                    ColumnSpec(
                        f"c{_}",
                        rng.choice(["int", "text"]),
                        descending=rng.choice([False, True]),
                        nulls_first=rng.choice([False, True]),
                    )
                )
            schema = KeySchema(columns)

            key_count = rng.randint(12, 36)
            keys = []
            for rowid in range(1, key_count + 1):
                values = []
                for column in columns:
                    chance = rng.random()
                    if chance < 0.18:
                        values.append(None)
                    elif column.kind == "int64":
                        values.append(rng.randint(-4, 4))
                    else:
                        values.append(rng.choice(text_values))
                keys.append(tuple(values + [rowid]))

            keys.sort(key=cmp_to_key(lambda a, b: compare_keys(a, b, schema)))

            leaves = []
            position = 0
            leaf_index = 0
            while position < len(keys):
                size = rng.randint(1, 4)
                chunk = keys[position : position + size]
                leaves.append(Leaf(chunk, f"case{case_number}-leaf{leaf_index}"))
                position += size
                leaf_index += 1

            tree = BTree(
                leaves,
                schema,
                fanout=rng.choice([2, 3, 4]),
                check_rowid_unique=True,
            )

            expected_leaf = {}
            for index, leaf in enumerate(leaves):
                for key in leaf.keys:
                    expected_leaf[key] = index
                    result = tree.lookup_leaf(key)
                    self.assertEqual(
                        result.leaf_index,
                        index,
                        msg=(
                            f"case {case_number}: {key} went to "
                            f"{result.leaf_index}, expected {index}"
                        ),
                    )
                    self.assertTrue(result.found)

            # Every adjacent boundary has a valid, globally shortest encoding.
            for index, (left, right) in enumerate(zip(leaves, leaves[1:])):
                lmax = left.maximum
                rmin = right.minimum
                selected = choose_separator(lmax, rmin, schema)
                self.assertLess(
                    compare_keys(lmax, selected.comparison_values(), schema),
                    0,
                )
                self.assertLessEqual(
                    compare_keys(selected.comparison_values(), rmin, schema),
                    0,
                )

                all_candidates = prefix_candidates(rmin, schema)
                valid = valid_separators(
                    lmax,
                    rmin,
                    schema,
                    candidates=all_candidates,
                )
                self.assertIn(selected, valid)
                self.assertEqual(
                    selected,
                    min(valid, key=separator_sort_key),
                    msg=f"case {case_number}, boundary {index}",
                )

                # The encoded length accounting agrees with its columns.
                self.assertEqual(
                    selected.byte_length,
                    sum(col.size for col in selected.encoded_columns),
                )
                self.assertEqual(
                    len(selected.to_bytes()),
                    selected.byte_length,
                )

                # A separator routes to the right leaf at the adjacent boundary.
                routed = tree.lookup_leaf(selected)
                self.assertEqual(routed.leaf_index, index + 1)

            self._assert_node_separator_invariants(tree.root, schema)

    def _assert_node_separator_invariants(self, node, schema):
        if node.is_leaf:
            return
        for index, separator in enumerate(node.separators):
            left_extreme = node.children[index].last_key
            right_extreme = node.children[index + 1].first_key
            self.assertLess(
                compare_keys(left_extreme, separator.comparison_values(), schema),
                0,
            )
            self.assertLessEqual(
                compare_keys(separator.comparison_values(), right_extreme, schema),
                0,
            )
        for child in node.children:
            self._assert_node_separator_invariants(child, schema)


if __name__ == "__main__":
    unittest.main()
