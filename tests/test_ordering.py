import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btree_separator import (  # noqa: E402
    NEG_INFINITY,
    ColumnSpec,
    KeySchema,
    compare_keys,
    validate_sorted_keys,
)


class OrderingTests(unittest.TestCase):
    def test_ascending_composite_with_rowid_tiebreak(self):
        schema = KeySchema([ColumnSpec("a", "int"), ColumnSpec("b", "text")])
        keys = [
            (1, "a", 1),
            (1, "a", 2),
            (1, "b", 1),
            (2, "a", 99),
        ]
        validate_sorted_keys(keys, schema)
        self.assertLess(compare_keys(keys[0], keys[1], schema), 0)
        self.assertLess(compare_keys(keys[1], keys[2], schema), 0)
        self.assertLess(compare_keys(keys[2], keys[3], schema), 0)

    def test_descending_integer_column(self):
        column = ColumnSpec("a", "int", descending=True)
        schema = KeySchema([column])
        keys = [(10, 1), (5, 2), (0, 3)]
        validate_sorted_keys(keys, schema)
        self.assertLess(compare_keys((5, 2), (3, 9), schema), 0)

    def test_descending_text_uses_reversed_raw_byte_order(self):
        schema = KeySchema([ColumnSpec("t", "text", descending=True)])
        keys = [("z", 1), ("banana", 2), ("apple", 3), ("", 4)]
        validate_sorted_keys(keys, schema)

        # Raw byte order is apple < apricot < banana; DESC reverses it.
        self.assertLess(
            compare_keys(("apricot", 2), ("apple", 3), schema),
            0,
        )

    def test_text_uses_raw_bytes_not_unicode_collation(self):
        schema = KeySchema([ColumnSpec("t", "text")])
        keys = [(b"a", 1), (b"ab", 2), (b"a\xff", 3)]
        validate_sorted_keys(keys, schema)

        # UTF-8 of "é" is c3 a9; a split byte prefix remains raw bytes.
        encoded_e_acute = "é".encode("utf-8")
        self.assertEqual(encoded_e_acute, b"\xc3\xa9")
        self.assertLess(
            compare_keys((b"\xc3", 1), (encoded_e_acute, 2), schema),
            0,
        )

    def test_nulls_first_and_last_are_independent_of_direction(self):
        asc_first = KeySchema([ColumnSpec("x", "int", nulls="first")])
        self.assertLess(compare_keys((None, 1), (-10**9, 2), asc_first), 0)

        asc_last = KeySchema([ColumnSpec("x", "int", nulls="last")])
        self.assertLess(compare_keys((-10**9, 2), (None, 1), asc_last), 0)

        desc_first = KeySchema(
            [ColumnSpec("x", "int", descending=True, nulls_first=True)]
        )
        self.assertLess(compare_keys((None, 1), (10**9, 2), desc_first), 0)

        desc_last = KeySchema(
            [ColumnSpec("x", "int", descending=True, nulls_first=False)]
        )
        # DESC makes 10 sort before 1, while NULLS LAST puts NULL after both.
        ordered = [(10, 1), (1, 2), (None, 3)]
        validate_sorted_keys(ordered, desc_last)

    def test_negative_infinity_is_below_null(self):
        schema = KeySchema([ColumnSpec("x", "int", nulls_first=True)])
        self.assertLess(compare_keys((NEG_INFINITY,), (None, 1), schema), 0)
        self.assertLess(compare_keys((), (None, 1), schema), 0)

    def test_unsorted_and_duplicate_keys_are_rejected(self):
        schema = KeySchema([ColumnSpec("x", "int")])
        with self.assertRaises(Exception):
            validate_sorted_keys([(2, 1), (1, 2)], schema)
        with self.assertRaises(Exception):
            validate_sorted_keys([(1, 1), (1, 1)], schema, strict=True)


if __name__ == "__main__":
    unittest.main()
