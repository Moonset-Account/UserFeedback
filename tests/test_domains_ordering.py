from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dictionary_reduction import (  # noqa: E402
    CASE_INSENSITIVE,
    BINARY,
    ComparableDomain,
    DictionarySegment,
    OrderDeclaration,
    OrderDeclarationError,
    reduce_segments,
)


class CollationAndDomainTests(unittest.TestCase):
    def setUp(self):
        self.segment = DictionarySegment(
            segment_id="mixed-case",
            dictionary=["Apple", "apple", "BANANA", "banana"],
            codes=[0, 1, 2, 3],
            measures=[1, 2, 4, 8],
            # Global order is deliberately unrelated to position/case.
            row_ids=[3, 0, 2, 1],
        )

    def test_binary_and_case_insensitive_collations_group_differently(self):
        binary_result = reduce_segments([self.segment], domain=BINARY)
        ci_result = reduce_segments([self.segment], domain=CASE_INSENSITIVE)

        self.assertEqual(
            binary_result.tuples(),
            {
                "Apple": (1, 3),
                "apple": (2, 0),
                "BANANA": (4, 2),
                "banana": (8, 1),
            },
        )
        self.assertEqual(
            ci_result.tuples(),
            {
                "apple": (3, 0),
                "banana": (12, 1),
            },
        )

        binary_order = [
            group.key for group in binary_result.ordered_groups("ascending")
        ]
        ci_order = [group.key for group in ci_result.ordered_groups("ascending")]
        self.assertEqual(binary_order, ["Apple", "BANANA", "apple", "banana"])
        self.assertEqual(ci_order, ["apple", "banana"])

        ci_desc = [
            group.key
            for group in ci_result.ordered_groups(OrderDeclaration.descending())
        ]
        self.assertEqual(ci_desc, ["banana", "apple"])

    def test_byte_values_follow_configured_collation(self):
        segment = DictionarySegment(
            dictionary=[b"Ab", b"ab"],
            codes=[0, 1],
            measures=[5, 7],
            row_ids=[1, 0],
        )
        binary = reduce_segments([segment], domain=BINARY)
        ci = reduce_segments([segment], domain=CASE_INSENSITIVE)

        self.assertEqual(len(binary.groups), 2)
        self.assertEqual(ci.tuples(), {b"ab": (12, 0)})

    def test_comparable_bounds_are_enforced(self):
        domain = ComparableDomain(
            "a_to_c",
            lower="a",
            upper="c",
            allowed_types=str,
        )
        valid = DictionarySegment(dictionary=["b"], codes=[0], row_ids=[0])
        result = reduce_segments([valid], domain=domain)
        self.assertEqual(result.by_key["b"].sum, 0)

        invalid = DictionarySegment(dictionary=["d"], codes=[0], row_ids=[1])
        with self.assertRaises(Exception):
            reduce_segments([invalid], domain=domain)

    def test_default_domain_supports_other_comparable_hashable_values(self):
        segment = DictionarySegment(
            dictionary=[10, 2, 10],
            codes=[0, 1, 2],
            measures=[1, 2, 3],
            row_ids=[2, 0, 1],
        )
        result = reduce_segments([segment])
        self.assertEqual(result.tuples(), {10: (4, 1), 2: (2, 0)})
        ordered = [group.key for group in result.ordered_groups("ascending")]
        self.assertEqual(ordered, [2, 10])

    def test_invalid_ascending_order_declaration_is_rejected(self):
        segment = DictionarySegment(
            dictionary=["b", "a"],
            codes=[0, 1],
            row_ids=[0, 1],
            ordering=OrderDeclaration.ascending(),
        )
        with self.assertRaises(OrderDeclarationError):
            reduce_segments([segment], verify_declarations=True)

        # Without verification, aggregation remains correct and does not use
        # the declaration as an assumption about first row.
        result = reduce_segments([segment], verify_declarations=False)
        self.assertEqual(result.by_key["a"].first_row, 1)

    def test_invalid_global_row_order_declaration_is_rejected(self):
        segment = DictionarySegment(
            dictionary=["a", "b", "c"],
            codes=[0, 1, 2],
            row_ids=[0, 2, 1],
            ordering=OrderDeclaration(
                direction="ascending", rows="ascending"
            ),
        )
        with self.assertRaises(OrderDeclarationError):
            segment.verify_order(BINARY)

    def test_null_order_declaration_boundaries(self):
        valid = DictionarySegment(
            dictionary=["a", "b"],
            codes=[-1, 0, 1, -1],
            nulls=[True, False, False, True],
            row_ids=[0, 1, 2, 3],
            ordering=OrderDeclaration.ascending(nulls="first"),
        )
        # Ascending with nulls=first is violated by trailing null.
        with self.assertRaises(OrderDeclarationError):
            valid.verify_order(BINARY)

        corrected = DictionarySegment(
            dictionary=["a", "b"],
            codes=[-1, 0, 1],
            nulls=[True, False, False],
            row_ids=[0, 1, 2],
            ordering=OrderDeclaration.ascending(nulls="first"),
        )
        corrected.verify_order(BINARY)


if __name__ == "__main__":
    unittest.main()
