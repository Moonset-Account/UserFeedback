from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dictionary_reduction import (  # noqa: E402
    CodeCollisionError,
    DictionaryReducer,
    DictionarySegment,
    LocalDictionary,
    StableDictionary,
)


class StableDictionaryTests(unittest.TestCase):
    def test_sparse_selected_codes_and_duplicate_aliases_are_stable(self):
        dictionary = StableDictionary({0: "a", 3: "a", 5: "b"})
        snapshot_before_new = dictionary.snapshot()

        # Existing canonical and non-canonical codes stay addressable.
        self.assertEqual(dictionary.intern("a"), 0)
        self.assertEqual(dictionary.value_at(3), "a")
        self.assertEqual(dictionary.intern("b"), 5)

        # New code follows the largest adopted code, never fills/moves slots.
        self.assertEqual(dictionary.intern("c"), 6)
        self.assertEqual(dictionary.value_at(0), "a")
        self.assertEqual(dictionary.value_at(3), "a")
        self.assertEqual(dictionary.value_at(5), "b")
        self.assertEqual(dictionary.value_at(6), "c")

        # The old snapshot does not gain code 6.
        self.assertEqual(snapshot_before_new.codes(), (0, 3, 5))
        self.assertFalse(snapshot_before_new.has_code(6))
        self.assertEqual(snapshot_before_new.value_at(3), "a")

    def test_code_cannot_be_rebound_to_another_value(self):
        dictionary = StableDictionary(["a", "b"])
        with self.assertRaises(CodeCollisionError):
            dictionary.adopt({0: "different"})

    def test_interleaved_batches_keep_decoding_after_each_new_segment(self):
        reducer = DictionaryReducer(selected_dictionary={10: "x", 20: "y"})

        segments = [
            DictionarySegment(
                segment_id="round-1",
                dictionary=["z", "x"],
                codes=[0, 1],
                measures=[1, 2],
                row_ids=[100, 101],
            ),
            DictionarySegment(
                segment_id="round-2",
                dictionary={0: "y", 4: "new"},
                codes=[0, 4, 0],
                measures=[3, 4, 5],
                row_ids=[2, 0, 1],
            ),
            DictionarySegment(
                segment_id="round-3",
                dictionary=["new", "z", "later"],
                codes=[0, 1, 2],
                measures=[6, 7, 8],
                row_ids=[20, 21, 22],
            ),
        ]

        emitted = []
        decoded_history = []
        for segment in segments:
            batch = reducer.add_segment(segment)
            emitted.append(batch)
            # Record every old batch after each dictionary extension.
            decoded_history.append([old.decode_codes() for old in emitted])

        self.assertEqual(emitted[0].codes, (21, 10))  # z is new after max 20
        self.assertEqual(emitted[1].codes, (20, 22, 20))  # y=20, new=22
        second_decoded = emitted[1].decode_codes()
        self.assertEqual(second_decoded, ("y", "new", "y"))
        new_code = emitted[1].codes[1]
        self.assertGreater(new_code, 20)

        self.assertEqual(emitted[2].decode_codes(), ("new", "z", "later"))

        # Old batches remain byte-for-byte the same code tuples and values.
        self.assertEqual(emitted[0].decode_codes(), ("z", "x"))
        self.assertEqual(emitted[1].decode_codes(), ("y", "new", "y"))
        self.assertEqual(decoded_history[0], [("z", "x")])
        self.assertEqual(
            decoded_history[1],
            [("z", "x"), ("y", "new", "y")],
        )
        self.assertEqual(
            decoded_history[2],
            [("z", "x"), ("y", "new", "y"), ("new", "z", "later")],
        )

        result = reducer.result()
        # Global first rows from round-2 beat round-1's high row ids.
        self.assertEqual(result.by_key["y"].first_row, 1)
        self.assertEqual(result.by_key["new"].first_row, 0)
        self.assertEqual(result.by_key["z"].first_row, 21)
        self.assertEqual(result.by_key["later"].first_row, 22)

    def test_snapshot_decode_uses_null_sentinel(self):
        dictionary = StableDictionary(["a"])
        snapshot = dictionary.snapshot()
        self.assertEqual(snapshot.decode([0, -1, None]), ("a", None, None))

    def test_dictionary_snapshot_can_be_selected_as_initial_dictionary(self):
        source = StableDictionary({2: "a", 5: "b"})
        snapshot = source.snapshot()
        selected = StableDictionary(snapshot)
        self.assertEqual(selected.intern("a"), 2)
        self.assertEqual(selected.intern("b"), 5)
        self.assertEqual(selected.intern("c"), 6)

    def test_named_local_dictionary_is_accepted_by_segment(self):
        local = LocalDictionary(["x", "y"])
        segment = DictionarySegment(
            dictionary=local,
            codes=[1, 0],
            measures=[3, 4],
            row_ids=[0, 1],
        )
        batch = DictionaryReducer().add_segment(segment)
        self.assertEqual(batch.decode_codes(), ("y", "x"))

    def test_aggregate_batch_codes_and_partial_values_are_stable(self):
        reducer = DictionaryReducer()
        first = DictionarySegment(
            dictionary=["a", "b"],
            codes=[0, 1, 0],
            measures=[1, 2, 4],
            row_ids=[10, 11, 12],
        )
        reducer.add_segment(first)
        partial = reducer.aggregate_batch(order="ascending")
        self.assertEqual(partial.decoded_keys(), ("a", "b"))
        self.assertEqual(partial.sums, (5, 2))
        partial_snapshot = partial.dictionary_snapshot

        second = DictionarySegment(
            dictionary=["b", "c"],
            codes=[0, 1, 0],
            measures=[20, 7, 40],
            row_ids=[0, 1, 2],
        )
        reducer.add_segment(second)
        final_batch = reducer.aggregate_batch(order="ascending")

        # New c receives a new group code; old group codes do not move.
        self.assertEqual(partial.group_codes, (0, 1))
        self.assertEqual(final_batch.group_codes, (0, 1, 2))
        self.assertEqual(final_batch.sums, (5, 62, 7))
        self.assertIs(partial.dictionary_snapshot, partial_snapshot)
        self.assertEqual(partial.dictionary_snapshot.values(), ("a", "b"))
        self.assertEqual(final_batch.decoded_keys(), ("a", "b", "c"))
        # The emitted partial tuple remains a point-in-time partial result.
        self.assertEqual(partial.sums, (5, 2))


if __name__ == "__main__":
    unittest.main()
