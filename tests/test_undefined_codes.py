import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solution import (  # noqa: E402
    InvalidPartitionError,
    Partition,
    UndefinedDictionaryCodeError,
    find_undefined_code,
    prepare_join,
    try_prepare_join,
)


class UndefinedCodeTests(unittest.TestCase):
    def test_left_undefined_sequence_code_reports_zero_and_one_based_location(self):
        left = [
            Partition(["a", "b"], [0, 1], [1, 2]),
            Partition(["c"], [0, 7, 0], [3, 4, 5], partition_id="bad"),
        ]
        right = [Partition(["a"], [0], [1])]

        report = find_undefined_code(left, right)
        self.assertIsNotNone(report)
        plan, returned_report = try_prepare_join(left, right)
        self.assertIsNone(plan)
        self.assertEqual(report, returned_report)
        self.assertEqual(report.side, "left")
        self.assertEqual(report.partition_index, 1)
        self.assertEqual(report.row_index, 1)
        self.assertEqual(report.code, 7)
        self.assertEqual(report.partition_id, "bad")
        self.assertEqual(report.partition, 1)
        self.assertEqual(report.row, 1)
        self.assertEqual(report.partition_number, 2)
        self.assertEqual(report.row_number, 2)

        with self.assertRaises(UndefinedDictionaryCodeError) as ctx:
            prepare_join(left, right)
        err = ctx.exception
        self.assertIs(err.details.partition_id, "bad")
        self.assertEqual(err.partition_index, 1)
        self.assertEqual(err.row_index, 1)
        self.assertIn("partition_index=1", str(err))
        self.assertIn("row #2", str(err))

    def test_right_undefined_code_is_reported_after_valid_left(self):
        left = [Partition(["a"], [0], [1])]
        right = [
            {"dictionary": ["a"], "codes": [0], "integer_keys": [1]},
            {
                "dictionary": ["b"],
                "codes": [0, 9],
                "integer_keys": [2, 3],
                "partition_id": "right-bad",
            },
        ]

        report = find_undefined_code(left, right)
        self.assertEqual(report.side, "right")
        self.assertEqual(report.partition_index, 1)
        self.assertEqual(report.row_index, 1)
        self.assertEqual(report.code, 9)
        self.assertEqual(report.partition_id, "right-bad")

    def test_undefined_code_is_reported_even_when_other_key_is_null(self):
        # The malformed dictionary code is still an undefined input code;
        # nullness only removes otherwise valid rows from matching.
        left = [Partition(["a"], [0, 4], [1, None])]
        right = [Partition(["a"], [0], [1])]

        report = find_undefined_code(left, right)
        self.assertIsNotNone(report)
        self.assertEqual((report.partition_index, report.row_index), (0, 1))
        self.assertEqual(report.code, 4)

    def test_negative_index_is_not_accepted_as_python_negative_indexing(self):
        left = [Partition(["a", "b"], [-2], [1])]
        right = [Partition(["a"], [0], [1])]

        report = find_undefined_code(
            left, right, code_null_values=(None,)
        )
        self.assertEqual(report.code, -2)
        self.assertEqual(report.row_index, 0)

    def test_mapping_dictionary_supports_named_codes_and_reports_missing(self):
        left = [
            Partition(
                {10: "a", 20: "b"},
                [10, 30],
                [1, 2],
            )
        ]
        right = [Partition({10: "a"}, [10], [1])]

        report = find_undefined_code(left, right)
        self.assertEqual(report.code, 30)
        self.assertEqual(report.row_index, 1)

        # Defined mapping codes join normally.
        left_valid = [Partition({10: "a", 20: "b"}, [10, 20], [1, 2])]
        plan = prepare_join(left_valid, right)
        self.assertEqual(len(plan.fetch_all()), 1)

    def test_shape_errors_are_distinct_from_undefined_codes(self):
        with self.assertRaises(InvalidPartitionError):
            prepare_join(
                [Partition(["a"], [0], [1, 2])],
                [Partition(["a"], [0], [1])],
            )

        with self.assertRaises(InvalidPartitionError):
            prepare_join(
                [{"codes": [0], "integer_keys": [1]}],
                [Partition(["a"], [0], [1])],
            )

        with self.assertRaises(InvalidPartitionError):
            prepare_join(
                [Partition([1, 2], [0], [1])],
                [Partition(["a"], [0], [1])],
            )

        with self.assertRaises(InvalidPartitionError):
            prepare_join(
                [Partition(["a"], [0], ["not-an-int"])],
                [Partition(["a"], [0], [1])],
            )


if __name__ == "__main__":
    unittest.main()
