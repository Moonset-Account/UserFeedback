import pathlib
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from rank_index import (  # noqa: E402
    Copy,
    CopyNotFoundError,
    InvalidRankError,
    RankIndex,
    r_row,
    s_row,
    t_row,
)
from rank_index import treap  # noqa: E402


class TrillionRankTest(unittest.TestCase):
    def setUp(self):
        self.db = RankIndex(
            R=[r_row(1, 1, 2, 10_000)],
            S=[s_row(2, 2, 3, 10_000)],
            T=[t_row(3, 3, 1, 10_000)],
        )
        self.snap = self.db.current

    def test_exact_trillion_total_and_boundary_copies(self):
        self.assertEqual(self.snap.total, 1_000_000_000_000)
        self.assertIsInstance(self.snap.total, int)

        first = self.snap.at(0)
        self.assertEqual((first.a, first.b, first.c), (1, 2, 3))
        self.assertEqual(first.triple, (1, 2, 3))
        self.assertEqual(first.reps, (1, 1, 1))

        last_rank = self.snap.total - 1
        last = self.snap.at(last_rank)
        self.assertEqual(last.reps, (10_000, 10_000, 10_000))
        self.assertEqual(self.snap.rank_of(last), last_rank)

    def test_sampled_ranks_reverse_exactly(self):
        for rank in [
            1,
            9_999,
            10_000,
            123_456_789,
            499_999_999_999,
            900_000_000_000,
            999_999_999_999,
        ]:
            with self.subTest(rank=rank):
                copy = self.snap.at(rank)
                self.assertEqual(self.snap.rank_of(copy), rank)

    def test_arbitrary_precision_with_90_digit_total(self):
        m = 10**30
        db = RankIndex(
            R=[r_row("r", "a", "b", m)],
            S=[s_row("s", "b", "c", m)],
            T=[t_row("t", "c", "a", m)],
        )
        snap = db.current
        self.assertEqual(snap.total, 10**90)
        last = snap.at(snap.total - 1)
        self.assertEqual(last.reps, (m, m, m))
        self.assertEqual(snap.rank_of(last), 10**90 - 1)

    def test_invalid_ranks_are_rejected(self):
        for bad in [-1, self.snap.total, self.snap.total + 3, True, "17"]:
            with self.subTest(bad=bad):
                with self.assertRaises((InvalidRankError, TypeError)):
                    self.snap.at(bad)

    def test_illegal_copy_references_are_rejected(self):
        good = self.snap.at(123)
        too_large_r = replace(good, r_rep=10_001)
        zero_t = replace(good, t_rep=0)
        wrong_row = replace(good, s_row=999)
        wrong_key = replace(good, c=30)
        for bad in [too_large_r, zero_t, wrong_row, wrong_key]:
            with self.subTest(bad=bad):
                with self.assertRaises(CopyNotFoundError):
                    self.snap.rank_of(bad)


class OrderingTest(unittest.TestCase):
    def test_key_rowid_and_replica_order_matches_lexicographic_spec(self):
        rows_r = [
            r_row(2, 1, 2, 2),
            r_row(1, 1, 2, 1),
            r_row(3, 0, 9, 1),  # no closure, must disappear
        ]
        rows_s = [s_row(10, 2, 3, 2), s_row(11, 2, 3, 1)]
        rows_t = [t_row(20, 3, 1, 1), t_row(21, 3, 1, 2)]
        snap = RankIndex(R=rows_r, S=rows_s, T=rows_t).current

        expected = []
        # Small synthetic input only: test-side brute force is tiny.
        for a, b, c in [(1, 2, 3)]:
            for rr in sorted([r for r in rows_r if r.values == (a, b)], key=lambda x: x.row_id):
                for ss in sorted([s for s in rows_s if s.values == (b, c)], key=lambda x: x.row_id):
                    for tt in sorted([t for t in rows_t if t.values == (c, a)], key=lambda x: x.row_id):
                        for ir in range(1, rr.multiplicity + 1):
                            for is_ in range(1, ss.multiplicity + 1):
                                for it in range(1, tt.multiplicity + 1):
                                    expected.append(
                                        Copy(
                                            a,
                                            b,
                                            c,
                                            rr.row_id,
                                            ss.row_id,
                                            tt.row_id,
                                            ir,
                                            is_,
                                            it,
                                        )
                                    )
        expected.sort(
            key=lambda x: (
                x.a,
                x.b,
                x.c,
                x.r_row,
                x.s_row,
                x.t_row,
                x.r_rep,
                x.s_rep,
                x.t_rep,
            )
        )
        self.assertEqual(snap.total, len(expected))
        actual = [snap.at(i) for i in range(snap.total)]
        self.assertEqual(actual, expected)
        for i, copy in enumerate(actual):
            self.assertEqual(snap.rank_of(copy), i)


class TupleInputTest(unittest.TestCase):
    def test_plain_tuple_rows_are_supported(self):
        snap = RankIndex(
            R=[(1, 1, 2, 3)],
            S=[(2, 2, 3, 4)],
            T=[(3, 3, 1, 5)],
        ).current
        self.assertEqual(snap.total, 60)
        last = snap.at(59)
        self.assertEqual(last.reps, (3, 4, 5))
        self.assertEqual(snap.rank_of(last), 59)


class MissingClosureEdgeTest(unittest.TestCase):
    def test_common_b_without_closing_t_edge_is_not_a_prefix_result(self):
        # Pseudo key would be (a=0,b=5,c=9) and sorts before the real key.
        db = RankIndex(
            R=[
                r_row("ghost-r", 0, 5),
                r_row("real-r", 1, 0),
            ],
            S=[
                s_row("ghost-s", 5, 9),  # shares b=5 with ghost-r
                s_row("real-s", 0, 0),
            ],
            T=[
                # Missing T(c=9,a=0): this is the only absent closure edge.
                t_row("real-t", 0, 1),
            ],
        )
        snap = db.current
        self.assertEqual(snap.keys, ((1, 0, 0),))
        self.assertEqual(snap.total, 1)
        # A ghost prefix before the real key would make this nonzero.
        self.assertEqual(snap.rank_before_key((1, 0, 0)), 0)
        self.assertEqual(snap.rank_before_key((2, 0, 0)), 1)
        self.assertEqual(snap.at(0).triple, ("real-r", "real-s", "real-t"))


class EmptyAndCursorBoundaryTest(unittest.TestCase):
    def test_empty_snapshot_and_end_cursor(self):
        snap = RankIndex().current
        self.assertEqual(snap.total, 0)
        self.assertEqual(snap.keys, ())
        cursor = snap.cursor(0)
        self.assertEqual(cursor.remaining, 0)
        self.assertEqual(cursor.fetch_many(10), [])
        with self.assertRaises(StopIteration):
            next(cursor)
        with self.assertRaises(InvalidRankError):
            snap.at(0)

    def test_treap_invariants_after_initial_build(self):
        snap = RankIndex(
            R=[r_row(i, i % 3, i % 2, 1 + i % 4) for i in range(20)],
            S=[s_row(i, i % 2, i % 3, 1 + i % 3) for i in range(15)],
            T=[t_row(i, i % 3, i % 3, 1 + i % 2) for i in range(18)],
        ).current
        treap.validate(snap.root)
        for rank in range(min(snap.total, 500)):
            self.assertEqual(snap.rank_of(snap.at(rank)), rank)


if __name__ == "__main__":
    unittest.main()
