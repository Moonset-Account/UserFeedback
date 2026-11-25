import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btree_separator import (  # noqa: E402
    ColumnSpec,
    KeySchema,
    SeparatorEncoder,
    TextPrefix,
    choose_separator,
    prefix_candidates,
    separator_sort_key,
    valid_separators,
)
from btree_separator.encoder import make_separator  # noqa: E402


class SeparatorEncoderTests(unittest.TestCase):
    def test_integer_and_terminal_negative_infinity_cost_nine_plus_one(self):
        schema = KeySchema([ColumnSpec("a", "int")])
        left = (0, 10)
        right = (1, 20)

        selected = choose_separator(left, right, schema)

        # R.a=1 distinguishes the boundaries; no rowid payload is needed.
        self.assertEqual(selected.values, (1,))
        self.assertTrue(selected.terminal_negative_infinity)
        self.assertEqual(selected.byte_length, 9 + 1)
        self.assertEqual(selected.encoded_column_count, 2)
        self.assertEqual([c.role for c in selected.encoded_columns],
                         ["integer", "negative_infinity"])
        self.assertEqual(len(selected.to_bytes()), selected.byte_length)
        self.assertTrue(selected.is_valid_for(left, right, schema))

    def test_null_has_tag_but_no_payload(self):
        schema = KeySchema([ColumnSpec("a", "int", nulls="last")])
        left = (7, 1)
        right = (None, 2)

        selected = choose_separator(left, right, schema)

        self.assertEqual(selected.values, (None,))
        self.assertEqual(selected.byte_length, 1 + 1)  # NULL tag + -inf tag
        roles = [c.role for c in selected.encoded_columns]
        self.assertEqual(roles, ["null", "negative_infinity"])
        self.assertEqual(selected.encoded_columns[0].payload, b"")

    def test_empty_text_has_only_its_tag(self):
        schema = KeySchema([ColumnSpec("t", "text", nulls="first")])
        left = (None, 1)
        right = ("", 2)

        selected = choose_separator(left, right, schema)

        self.assertEqual(selected.values, (b"",))
        # Empty text tag + terminal negative-infinity tag.
        self.assertEqual(selected.byte_length, 2)
        self.assertEqual(selected.encoded_columns[0].payload, b"")
        self.assertTrue(selected.is_valid_for(left, right, schema))

    def test_text_byte_prefix_is_selected(self):
        schema = KeySchema([ColumnSpec("t", "text")])
        left = (b"abz", 1)
        right = (b"acxxxx", 2)

        selected = choose_separator(left, right, schema)

        self.assertIsInstance(selected.values[0], TextPrefix)
        self.assertEqual(selected.values[0], b"ac")
        # Text tag (1) + two prefix bytes + -inf tag (1).
        self.assertEqual(selected.byte_length, 4)
        self.assertTrue(selected.is_valid_for(left, right, schema))

        # Every selected encoding must be the shortest among all valid choices.
        valid = valid_separators(left, right, schema)
        self.assertEqual(selected, min(valid, key=separator_sort_key))

    def test_utf8_byte_can_be_split_by_prefix(self):
        schema = KeySchema([ColumnSpec("t", "text")])
        right = ("é", 7)  # UTF-8: c3 a9
        raw_prefixes = []
        for candidate in prefix_candidates(right, schema):
            if candidate.values and isinstance(candidate.values[0], bytes):
                raw_prefixes.append(bytes(candidate.values[0]))
        self.assertIn(b"", raw_prefixes)
        self.assertIn(b"\xc3", raw_prefixes)
        self.assertIn(b"\xc3\xa9", raw_prefixes)

    def test_rowid_only_difference_requires_eight_byte_rowid(self):
        schema = KeySchema([ColumnSpec("a", "int")])
        left = (7, 100)
        right = (7, 101)

        selected = choose_separator(left, right, schema)

        self.assertEqual(selected.kind, "full_right_key")
        self.assertEqual(selected.values, (7, 101))
        # Integer tag+payload (9) and rowid tag+payload (9).
        self.assertEqual(selected.byte_length, 18)
        self.assertEqual(selected.encoded_column_count, 2)
        self.assertEqual([c.role for c in selected.encoded_columns],
                         ["integer", "rowid"])

        # A boundary before the rowid is negative infinity and is not greater
        # than the left rowid 100.
        boundary = next(
            c for c in prefix_candidates(right, schema)
            if c.terminal_negative_infinity and c.value_column_count == 1
        )
        self.assertFalse(boundary.is_valid_for(left, right, schema))

    def test_descending_text_rejects_every_proper_raw_prefix(self):
        schema = KeySchema([ColumnSpec("t", "text", descending=True)])
        left = ("apricot", 2)
        right = ("apple", 3)

        selected = choose_separator(left, right, schema)
        self.assertEqual(selected.values, (b"apple",))
        # Five text bytes + text tag + terminal -inf tag.
        self.assertEqual(selected.byte_length, 7)

        for candidate in prefix_candidates(right, schema):
            if candidate.kind != "text_byte_prefix":
                continue
            prefix = bytes(candidate.values[0])
            if len(prefix) < len(b"apple"):
                self.assertFalse(
                    candidate.is_valid_for(left, right, schema),
                    msg=f"proper DESC prefix unexpectedly valid: {prefix!r}",
                )

    def test_tie_break_prefers_fewer_encoded_columns(self):
        schema = KeySchema([
            ColumnSpec("a", "text"),
            ColumnSpec("b", "text"),
        ])
        shorter_columns = make_separator(
            [TextPrefix(b"12345678")],
            schema,
            terminal_negative_infinity=True,
            kind="text_byte_prefix",
        )
        more_columns = make_separator(
            [None, TextPrefix(b"1234567")],
            schema,
            terminal_negative_infinity=True,
            kind="synthetic_tie",
        )

        self.assertEqual(shorter_columns.byte_length, more_columns.byte_length)
        self.assertLess(
            shorter_columns.encoded_column_count,
            more_columns.encoded_column_count,
        )
        best = min([more_columns, shorter_columns], key=separator_sort_key)
        self.assertIs(best, shorter_columns)

    def test_rowid_only_schema(self):
        schema = KeySchema([])
        selected = choose_separator((41,), (42,), schema)
        self.assertEqual(selected.kind, "full_right_key")
        self.assertEqual(selected.byte_length, 9)
        self.assertEqual(selected.values, (42,))

    def test_encoder_class_and_invalid_boundary(self):
        schema = KeySchema([ColumnSpec("a", "int")])
        encoder = SeparatorEncoder(schema)
        selected = encoder.encode((0, 1), (1, 2))
        self.assertEqual(selected, choose_separator((0, 1), (1, 2), schema))

        with self.assertRaises(Exception):
            encoder.encode((2, 1), (1, 2))


if __name__ == "__main__":
    unittest.main()
