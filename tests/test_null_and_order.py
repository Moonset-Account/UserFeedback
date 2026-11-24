import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solution import Partition, join_all, prepare_join  # noqa: E402


def coords(matches):
    return [(m.left.coordinate, m.right.coordinate) for m in matches]


class NullAndOrderTests(unittest.TestCase):
    def test_none_and_minus_one_codes_skip_but_falsy_keys_do_not(self):
        left = [
            Partition(
                dictionary=["", "a"],
                codes=[0, 1, None, -1, 1],
                integer_keys=[0, None, 1, 0, 1],
            )
        ]
        right = [
            Partition(
                dictionary=["", "a"],
                codes=[0, 1, 1, None],
                integer_keys=[0, 1, 1, 0],
            )
        ]

        matches = prepare_join(left, right).fetch_all()

        expected = [
            ((0, 0), (0, 0)),  # empty text and integer zero are real keys
            ((0, 4), (0, 1)),
            ((0, 4), (0, 2)),
        ]
        self.assertEqual(coords(matches), expected)

    def test_minus_one_can_be_treated_as_undefined_when_configured(self):
        left = [Partition(["a"], [0, -1], [1, 1])]
        right = [Partition(["a"], [0], [1])]

        with self.assertRaises(Exception) as ctx:
            prepare_join(left, right, code_null_values=(None,))
        self.assertEqual(ctx.exception.details.row_index, 1)
        self.assertEqual(ctx.exception.details.code, -1)

    def test_integer_minus_one_is_an_ordinary_key_by_default(self):
        rows = join_all(
            [Partition(["a"], [0, 0], [-1, None])],
            [Partition(["a"], [0], [-1])],
        )
        self.assertEqual(coords(rows), [((0, 0), (0, 0))])

    def test_requires_both_text_class_and_integer_key_equality(self):
        left = [Partition(["a", "b", "a"], [0, 1, 0], [1, 1, 2])]
        right = [
            Partition(
                ["a", "b"],
                [0, 0, 0, 0],
                [2, 9, 1, 2],
            )
        ]

        matches = join_all(left, right)

        # a/1 only matches right row 2; a/2 matches right rows 0 and 3.
        expected = [
            ((0, 0), (0, 2)),
            ((0, 2), (0, 0)),
            ((0, 2), (0, 3)),
        ]
        self.assertEqual(coords(matches), expected)

    def test_duplicate_rows_on_both_sides_produce_full_inner_product(self):
        left = [Partition(["x"], [0, 0, 0], [5, 5, 5])]
        right = [Partition(["x"], [0, 0], [5, 5])]

        matches = join_all(left, right)
        self.assertEqual(len(matches), 6)
        self.assertEqual(
            coords(matches),
            [
                ((0, 0), (0, 0)),
                ((0, 0), (0, 1)),
                ((0, 1), (0, 0)),
                ((0, 1), (0, 1)),
                ((0, 2), (0, 0)),
                ((0, 2), (0, 1)),
            ],
        )

    def test_partition_and_row_order_are_canonical_input_order(self):
        left = [
            Partition(["k"], [0], [1], partition_id="l0"),
            Partition(["k"], [0, 0], [1, 1], partition_id="l1"),
        ]
        right = [
            Partition(["k"], [0], [1], partition_id="r0"),
            Partition(["k"], [0], [1], partition_id="r1"),
        ]

        matches = prepare_join(left, right).fetch_all()

        self.assertEqual(
            coords(matches),
            [
                ((0, 0), (0, 0)),
                ((0, 0), (1, 0)),
                ((1, 0), (0, 0)),
                ((1, 0), (1, 0)),
                ((1, 1), (0, 0)),
                ((1, 1), (1, 0)),
            ],
        )
        first = matches[0]
        self.assertEqual(first.left.partition_id, "l0")
        self.assertEqual(first.right.partition_id, "r0")
        self.assertEqual(first.left.row_number, 1)
        self.assertEqual(first.right.partition_number, 1)

    def test_empty_partitions_and_no_matches_are_safe(self):
        plan = prepare_join(
            [Partition([], [], [])],
            [Partition(["a"], [0], [1])],
        )
        batch = plan.fetch_batch()
        self.assertEqual(batch.pairs, ())
        self.assertTrue(batch.done)
        self.assertTrue(batch.next_cursor.done)

        no_match = join_all(
            [Partition(["a"], [0], [1])],
            [Partition(["b"], [0], [2])],
        )
        self.assertEqual(no_match, ())


if __name__ == "__main__":
    unittest.main()
