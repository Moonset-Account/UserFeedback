from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dictionary_reduction import (  # noqa: E402
    CodeOutOfRangeError,
    DictionaryReducer,
    DictionarySegment,
    DuplicateGlobalRowError,
    OrderDeclaration,
    normalize_null_markers,
    reduce_segments,
)


class ReductionSemanticsTests(unittest.TestCase):
    def test_local_codes_are_namespaced_and_global_first_row_wins(self):
        # Process the high-numbered rows first.  Local code 0 means "alpha" in
        # s1 but "beta" in s2; reversing that mapping is the trap this test
        # guards against.
        s1 = DictionarySegment(
            segment_id="s1",
            dictionary=["alpha", "beta"],
            codes=[0, 1, 0],
            measures=[1, 2, 4],
            row_ids=[10, 11, 12],
        )
        s2 = DictionarySegment(
            segment_id="s2",
            dictionary=["beta", "alpha"],
            # row ids are deliberately not in processing/position order
            codes=[1, 0, 1, 0],
            measures=[10, 20, 30, 40],
            row_ids=[3, 0, 1, 2],
        )

        result = reduce_segments([s1, s2])

        self.assertEqual(
            result.tuples(),
            {
                "alpha": (45, 1),
                "beta": (62, 0),
            },
        )
        alpha = result.by_key["alpha"]
        beta = result.by_key["beta"]
        self.assertEqual(alpha.count, 4)
        self.assertEqual(beta.count, 3)
        self.assertIsNotNone(alpha.first_source)
        self.assertEqual(alpha.first_source.segment_id, "s2")
        self.assertEqual(alpha.first_source.row_index, 2)
        self.assertEqual(beta.first_source.segment_id, "s2")
        self.assertEqual(beta.first_source.row_index, 1)

    def test_selected_dictionary_reuses_codes_and_new_values_append(self):
        selected = ["beta", "alpha"]  # beta=0, alpha=1
        reducer = DictionaryReducer(selected_dictionary=selected)

        first = DictionarySegment(
            segment_id="first",
            dictionary=["alpha", "beta"],
            codes=[0, 1],
            measures=[7, 9],
            row_ids=[5, 6],
        )
        first_batch = reducer.add_segment(first)

        self.assertEqual(first_batch.codes, (1, 0))
        self.assertEqual(first_batch.dictionary_snapshot.value_at(0), "beta")
        self.assertEqual(first_batch.dictionary_snapshot.value_at(1), "alpha")
        old_snapshot = first_batch.dictionary_snapshot

        second = DictionarySegment(
            segment_id="second",
            dictionary={7: "gamma", 9: "alpha"},
            codes=[7, 9, 7],
            measures=[1, 2, 4],
            row_ids=[0, 1, 2],
        )
        second_batch = reducer.add_segment(second)

        # New value gets a fresh code after selected code 1.
        self.assertEqual(second_batch.codes, (2, 1, 2))
        self.assertEqual(reducer.output_dictionary.value_at(0), "beta")
        self.assertEqual(reducer.output_dictionary.value_at(1), "alpha")
        self.assertEqual(reducer.output_dictionary.value_at(2), "gamma")

        # The already emitted batch and its point-in-time snapshot are unchanged.
        self.assertEqual(first_batch.codes, (1, 0))
        self.assertEqual(first_batch.decode_codes(), ("alpha", "beta"))
        self.assertIs(first_batch.dictionary_snapshot, old_snapshot)
        self.assertEqual(old_snapshot.values(), ("beta", "alpha"))
        self.assertEqual(reducer.output_dictionary.snapshot().value_at(1), "alpha")

        result = reducer.result()
        self.assertEqual(result.by_key["gamma"].sum, 5)
        self.assertEqual(result.by_key["alpha"].sum, 7 + 2)
        self.assertEqual(result.by_key["beta"].sum, 9)

    def test_duplicate_dictionary_entries_share_one_group(self):
        segment = DictionarySegment(
            segment_id="dup",
            dictionary={0: "same", 3: "same", 8: "other", 9: "same"},
            codes=[0, 3, 8, 9],
            measures=[1, 2, 3, 4],
            row_ids=[100, 1, 2, 0],
        )
        result = reduce_segments([segment])
        self.assertEqual(result.tuples(), {"same": (7, 0), "other": (3, 2)})
        same = result.by_key["same"]
        self.assertEqual(same.count, 3)
        # Exact output dictionary canonicalizes duplicate physical values.
        self.assertEqual(same.output_codes, {0})

    def test_interleaved_nulls_and_invalid_null_code_are_allowed(self):
        segment = DictionarySegment(
            segment_id="nulls",
            dictionary=["a", "b"],
            codes=[0, -999, 1, "don't-care-null-code", 0],
            nulls=[False, True, False, True, False],
            measures=[2, 7, 3, 11, 5],
            row_ids=[4, 1, 3, 0, 2],
        )
        result = reduce_segments([segment])

        self.assertEqual(set(result.by_key), {"a", "b", None})
        self.assertEqual(result.by_key["a"].sum, 7)
        self.assertEqual(result.by_key["a"].first_row, 2)
        self.assertEqual(result.by_key["b"].sum, 3)
        self.assertEqual(result.by_key["b"].first_row, 3)
        null_group = result.by_key[None]
        self.assertTrue(null_group.is_null)
        self.assertEqual(null_group.sum, 18)
        self.assertEqual(null_group.count, 2)
        self.assertEqual(null_group.first_row, 0)
        self.assertEqual(null_group.first_source.row_index, 3)

        batch = result.batches[0]
        self.assertEqual(batch.indices, (0, -1, 1, -1, 0))
        self.assertEqual(batch.decode_codes(), ("a", None, "b", None, "a"))

    def test_integer_boundaries_negative_and_large_values(self):
        large = 10**40
        segment = DictionarySegment(
            dictionary=["x"],
            codes=[0, 0, 0],
            measures=[large, -large - 1, 1],
            row_ids=[2**63 + 10, 0, 1],
        )
        result = reduce_segments([segment])
        group = result.by_key["x"]
        self.assertEqual(group.sum, 0)
        self.assertEqual(group.first_row, 0)

    def test_empty_and_null_only_segments(self):
        empty = DictionarySegment(
            segment_id="empty",
            dictionary=[],
            codes=[],
            row_ids=[],
        )
        null_only = DictionarySegment(
            segment_id="null-only",
            dictionary=None,
            codes=[-1, -1],
            nulls=0b11,
            measures=[4, 6],
            row_ids=[9, 3],
        )
        result = reduce_segments([empty, null_only])
        self.assertEqual(result.row_count, 2)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.by_key[None].sum, 10)
        self.assertEqual(result.by_key[None].first_row, 3)

    def test_missing_non_null_code_raises_and_leaves_reducer_usable(self):
        reducer = DictionaryReducer()
        good = DictionarySegment(
            segment_id="good",
            dictionary=["ok"],
            codes=[0],
            measures=[5],
            row_ids=[0],
        )
        reducer.add_segment(good)

        bad = DictionarySegment(
            segment_id="bad",
            dictionary=["ok"],
            codes=[0, 7],
            nulls=[False, False],
            measures=[1, 2],
            row_ids=[1, 2],
        )
        with self.assertRaises(CodeOutOfRangeError):
            reducer.add_segment(bad)

        result = reducer.result()
        self.assertEqual(len(result.batches), 1)
        self.assertEqual(result.tuples(), {"ok": (5, 0)})

    def test_duplicate_global_row_is_rejected(self):
        s1 = DictionarySegment(
            segment_id="s1", dictionary=["a"], codes=[0], row_ids=[7]
        )
        s2 = DictionarySegment(
            segment_id="s2", dictionary=["b"], codes=[0], row_ids=[7]
        )
        reducer = DictionaryReducer(strict_row_ids=True)
        reducer.add_segment(s1)
        with self.assertRaises(DuplicateGlobalRowError):
            reducer.add_segment(s2)

        # Replaying the exact same source position is also a duplicate global
        # row under strict global identity.
        with self.assertRaises(DuplicateGlobalRowError):
            reducer.add_segment(s1)

    def test_null_group_can_be_excluded(self):
        segment = DictionarySegment(
            dictionary=["a"],
            codes=[0, -1, 0],
            nulls=[False, True, False],
            measures=[2, 100, 3],
            row_ids=[0, 1, 2],
        )
        included = reduce_segments([segment], null_group=True)
        excluded = reduce_segments([segment], null_group=False)

        self.assertIsNotNone(included.by_key[None])
        self.assertEqual(included.by_key[None].sum, 100)
        self.assertEqual(set(excluded.by_key), {"a"})
        self.assertEqual(excluded.by_key["a"].sum, 5)
        self.assertEqual(excluded.row_count, 3)

    def test_reduce_method_and_call_alias(self):
        segment = DictionarySegment(
            dictionary=["a"], codes=[0, 0], measures=[2, 3], row_ids=[1, 0]
        )
        reducer = DictionaryReducer()
        result = reducer.reduce([segment])
        self.assertEqual(result.tuples(), {"a": (5, 0)})

        called_result = DictionaryReducer()([segment])
        self.assertEqual(called_result.tuples(), {"a": (5, 0)})

    def test_order_declaration_does_not_change_global_first_selection(self):
        # Values are ascending even though global row ids are deliberately
        # out of order.  rows="any" declares no global-row ordering.
        segment = DictionarySegment(
            dictionary=["a", "b", "c"],
            codes=[0, 1, 2],
            measures=[1, 1, 1],
            row_ids=[30, 10, 20],
            ordering=OrderDeclaration.ascending(rows="any"),
        )
        result = reduce_segments([segment], verify_declarations=True)
        self.assertEqual(result.by_key["a"].first_row, 30)
        self.assertEqual(result.by_key["b"].first_row, 10)
        self.assertEqual(result.by_key["c"].first_row, 20)

    def test_null_bitmap_integer_and_packed_bytes(self):
        segment = DictionarySegment(
            dictionary=["a", "b", "c"],
            codes=[0, 1, 2, 0, 1],
            nulls=0b01010,  # positions 1 and 3 are null
            row_ids=[0, 1, 2, 3, 4],
        )
        self.assertEqual(segment.nulls, (False, True, False, True, False))
        self.assertEqual(normalize_null_markers(True, 3), (True, True, True))
        self.assertEqual(normalize_null_markers(False, 2), (False, False))

        packed = DictionarySegment(
            dictionary=["a"],
            codes=[0, 0, 0, 0, 0, 0, 0, 0, 0],
            nulls=bytes([0b00000010, 0b1]),  # bit 1 and bit 8
            row_ids=range(9),
        )
        self.assertTrue(packed.nulls[1])
        self.assertTrue(packed.nulls[8])
        self.assertFalse(packed.nulls[0])


if __name__ == "__main__":
    unittest.main()
