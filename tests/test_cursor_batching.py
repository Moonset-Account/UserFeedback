import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solution import (  # noqa: E402
    InvalidCursorError,
    JoinCursor,
    Partition,
    join_batch,
    prepare_join,
)


def coords(matches):
    return [(m.left.coordinate, m.right.coordinate) for m in matches]


class CursorBatchingTests(unittest.TestCase):
    def setUp(self):
        self.left = [
            Partition(["a", "b"], [0, 1, 0], [1, 2, 1]),
            Partition(["b", "c"], [0, 1, 0], [2, 3, 9]),
        ]
        self.right = [
            Partition(["B", "a"], [0, 1, 1], [2, 1, 1]),
            Partition(["a", "z"], [0, 0], [1, 7]),
        ]
        self.plan = prepare_join(
            self.left, self.right, text_comparison="casefold"
        )
        self.expected = coords(self.plan.fetch_all())
        self.assertEqual(len(self.expected), 8)

    def _resume_with_size(self, size, use_tokens=False):
        collected = []
        cursor = self.plan.cursor(size)
        batches = 0
        while True:
            resume = cursor.token if use_tokens and batches else cursor
            batch = self.plan.fetch_batch(resume)
            batches += 1
            collected.extend(coords(batch.pairs))
            cursor = batch.next_cursor
            if batch.done:
                break
        return collected, batches

    def test_batch_size_one_resumes_through_every_pair(self):
        collected, batches = self._resume_with_size(1)
        self.assertEqual(collected, self.expected)
        self.assertEqual(batches, 8)

    def test_batch_size_two_preserves_order_and_exact_batch_count(self):
        collected, batches = self._resume_with_size(2)
        self.assertEqual(collected, self.expected)
        self.assertEqual(batches, 4)

    def test_token_resume_across_pauses(self):
        collected, batches = self._resume_with_size(3, use_tokens=True)
        self.assertEqual(collected, self.expected)
        # Eight matches / three => batches of sizes 3, 3, 2.
        self.assertEqual(batches, 3)

    def test_final_batch_is_marked_done(self):
        batch = self.plan.fetch_batch(batch_size=5)
        self.assertEqual(len(batch), 5)
        self.assertFalse(batch.done)

        final = self.plan.fetch_batch(batch.next_cursor.token)
        self.assertEqual(len(final), 3)
        self.assertTrue(final.done)
        self.assertTrue(final.next_cursor.done)

        again = self.plan.fetch_batch(final.next_cursor)
        self.assertEqual(len(again), 0)
        self.assertTrue(again.done)

    def test_same_cursor_is_immutable_and_can_be_replayed(self):
        first = self.plan.fetch_batch(batch_size=2)
        replay = self.plan.fetch_batch(first.next_cursor)
        replay_token = self.plan.fetch_batch(first.next_cursor.token)

        self.assertEqual(coords(replay.pairs), coords(replay_token.pairs))
        self.assertEqual(replay.next_cursor, replay_token.next_cursor)

        continued = self.plan.fetch_batch(replay.next_cursor)
        self.assertEqual(len(continued), 2)
        self.assertNotEqual(coords(first.pairs), coords(replay.pairs))

    def test_interleaved_cursors_do_not_disturb_each_other(self):
        cursor_small = self.plan.cursor(2)
        cursor_large = self.plan.cursor(3)
        small = []
        large = []

        for turn in range(4):
            batch_small = self.plan.fetch_batch(cursor_small)
            small.extend(coords(batch_small.pairs))
            cursor_small = batch_small.next_cursor

            if turn < 3:
                batch_large = self.plan.fetch_batch(cursor_large)
                large.extend(coords(batch_large.pairs))
                cursor_large = batch_large.next_cursor

        # Drain the size-3 cursor.
        while not batch_large.done:
            batch_large = self.plan.fetch_batch(cursor_large)
            large.extend(coords(batch_large.pairs))
            cursor_large = batch_large.next_cursor

        self.assertEqual(small, self.expected)
        self.assertEqual(large, self.expected)

    def test_resume_inside_right_fanout_keeps_right_order(self):
        # Batch boundary after the first two of three a/1 right matches.
        first = self.plan.fetch_batch(batch_size=2)
        self.assertEqual(
            coords(first.pairs), self.expected[:2]
        )
        self.assertEqual(first.next_cursor.left_index, 0)
        self.assertEqual(first.next_cursor.right_offset, 2)

        second = self.plan.fetch_batch(first.next_cursor)
        self.assertEqual(coords(second.pairs)[:2], self.expected[2:4])

        all_coords, _ = self._resume_with_size(2)
        self.assertEqual(all_coords, self.expected)

    def test_invalid_batch_size_and_cursor_token(self):
        with self.assertRaises(InvalidCursorError):
            JoinCursor(batch_size=0)
        with self.assertRaises(ValueError):
            self.plan.fetch_batch(batch_size=0)
        with self.assertRaises(InvalidCursorError):
            JoinCursor.from_token("not-a-real-token")
        with self.assertRaises(InvalidCursorError):
            JoinCursor.from_token(
                "columnar-join-cursor-v1." + "%%%not-base64%%%"
            )

    def test_functional_join_batch_can_resume_from_token(self):
        first = join_batch(
            self.left,
            self.right,
            batch_size=4,
            text_comparison="casefold",
        )
        second = join_batch(
            self.left,
            self.right,
            first.next_cursor.token,
            text_comparison="casefold",
        )
        self.assertEqual(len(first), 4)
        self.assertEqual(len(second), 4)
        self.assertEqual(
            coords(first.pairs) + coords(second.pairs), self.expected
        )


if __name__ == "__main__":
    unittest.main()
