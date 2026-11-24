import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solution import Partition, join_all, prepare_join  # noqa: E402


class InputAdapterTests(unittest.TestCase):
    def test_mapping_partitions_and_field_aliases(self):
        left = [
            {
                "text_dictionary": ["a"],
                "row_codes": [0, None],
                "second_keys": [1, 2],
                "name": "left-name",
            }
        ]
        right = [
            {
                "dictionary": ["a"],
                "codes": [0],
                "integer_keys": [1],
            }
        ]

        plan = prepare_join(left, right)
        self.assertEqual(plan.left_partitions[0].partition_id, "left-name")
        matches = plan.fetch_all()
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].left.partition_id, "left-name")

    def test_bare_partition_tuple_is_accepted(self):
        left = (["a", "b"], [0, 1], [1, 2], "tuple-left")
        right = [(["b", "a"], [1, 0], [1, 2])]

        matches = join_all(left, right)
        self.assertEqual(
            [(m.left.coordinate, m.right.coordinate) for m in matches],
            [((0, 0), (0, 0)), ((0, 1), (0, 1))],
        )
        self.assertEqual(matches[0].left.partition_id, "tuple-left")

    def test_outer_mapping_of_partition_ids_is_accepted(self):
        left = {
            "l0": Partition(["a"], [0], [1]),
            "l1": (["a"], [0], [1]),
        }
        right = {"r0": {"dictionary": ["a"], "codes": [0], "integer_keys": [1]}}

        plan = prepare_join(left, right)
        self.assertEqual(
            [p.partition_id for p in plan.left_partitions], ["l0", "l1"]
        )
        matches = plan.fetch_all()
        self.assertEqual(len(matches), 2)
        self.assertEqual(
            [m.left.partition_id for m in matches], ["l0", "l1"]
        )
        self.assertEqual(matches[0].right.partition_id, "r0")

    def test_single_partition_object_is_accepted(self):
        from solution import Partition

        matches = join_all(
            Partition(["a"], [0], [1]),
            Partition(["a"], [0], [1]),
        )
        self.assertEqual(len(matches), 1)

    def test_match_unpacks_as_plain_coordinate_pairs(self):
        matches = join_all(
            [Partition(["a", "b"], [0, 1], [1, 2])],
            [Partition(["b", "a"], [0, 1], [2, 1])],
        )

        self.assertEqual(
            matches,
            (((0, 0), (0, 1)), ((0, 1), (0, 0))),
        )
        (left_partition, left_row), (right_partition, right_row) = matches[0]
        self.assertEqual((left_partition, left_row), (0, 0))
        self.assertEqual((right_partition, right_row), (0, 1))
        self.assertEqual(matches[0].left.global_index, 0)
        self.assertEqual(matches[0].right.row_number, 2)
        with self.assertRaises(AttributeError):
            matches[0].left.row_index = 9


if __name__ == "__main__":
    unittest.main()
