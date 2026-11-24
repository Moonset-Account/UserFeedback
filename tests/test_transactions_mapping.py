import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from rank_index import (  # noqa: E402
    AddedRankError,
    ConcurrentUpdateError,
    DeletedRankError,
    RankIndex,
    r_row,
    s_row,
    t_row,
)
from rank_index.groups import KeyGroup, build_group as real_build_group  # noqa: E402


def triangle_db(m=10_000):
    return RankIndex(
        R=[r_row("r", 1, 2, m)],
        S=[s_row("s", 2, 3, m)],
        T=[t_row("t", 3, 1, m)],
    )


class TrillionSnapshotAndMappingTest(unittest.TestCase):
    def test_old_index_survives_single_row_multiplicity_change(self):
        db = triangle_db(10_000)
        old = db.current
        pinned = old.cursor(899_999_999_999)
        old_last = old.at(old.total - 1)

        mapping = db.set_multiplicity("R", "r", 9_000)
        new = db.current

        # Both rank spaces remain exact and independently usable.
        self.assertEqual(old.total, 1_000_000_000_000)
        self.assertEqual(new.total, 900_000_000_000)
        self.assertEqual(old.at(old.total - 1), old_last)
        self.assertEqual(old.rank_of(old_last), old.total - 1)
        self.assertEqual(pinned.get().reps, (9_000, 10_000, 10_000))

        self.assertEqual(mapping.deleted_old_count, 100_000_000_000)
        self.assertEqual(mapping.retained_count, 900_000_000_000)
        self.assertEqual(mapping.added_new_count, 0)
        self.assertEqual(
            mapping.deleted_old_ranges.merged_intervals(),
            ((900_000_000_000, 1_000_000_000_000),),
        )

        self.assertEqual(mapping.old_to_new(0), 0)
        self.assertEqual(mapping.old_to_new(899_999_999_999), 899_999_999_999)
        for deleted_rank in [900_000_000_000, 999_999_999_999]:
            result = mapping.locate_old(deleted_rank)
            self.assertTrue(result.deleted)
            self.assertEqual(
                result.old_interval, (900_000_000_000, 1_000_000_000_000)
            )
            with self.assertRaises(DeletedRankError):
                mapping.old_to_new(deleted_rank)

        # The pinned cursor can continue reading the old rank space.
        copy = pinned.advance()
        self.assertEqual(old.rank_of(copy), 900_000_000_000)
        self.assertEqual(copy.reps, (9_001, 1, 1))

    def test_middle_dimension_shrink_is_compressed_strided_intervals(self):
        db = RankIndex(
            R=[r_row("r", 1, 2, 2)],
            S=[s_row("s", 2, 3, 3)],
            T=[t_row("t", 3, 1, 4)],
        )
        old = db.current
        mapping = db.set_multiplicity("S", "s", 2)
        new = db.current

        self.assertEqual(old.total, 24)
        self.assertEqual(new.total, 16)
        self.assertEqual(
            mapping.deleted_old_ranges.merged_intervals(), ((8, 12), (20, 24))
        )
        for rank in range(old.total):
            result = mapping.locate_old(rank)
            if result.retained:
                self.assertEqual(mapping.old_to_new(rank), result.new_rank)
                self.assertEqual(new.at(result.new_rank), old.at(rank))
            else:
                interval = mapping.deleted_interval_containing(rank)
                self.assertIn(rank, range(*interval))

    def test_fast_dimension_growth_creates_added_intervals_and_reverse_map(self):
        db = RankIndex(
            R=[r_row("r", 1, 2, 1)],
            S=[s_row("s", 2, 3, 1)],
            T=[t_row("t", 3, 1, 2)],
        )
        old = db.current
        mapping = db.set_multiplicity("T", "t", 4)
        new = db.current
        self.assertEqual((old.total, new.total), (2, 4))
        self.assertEqual(mapping.added_new_ranges.merged_intervals(), ((2, 4),))
        self.assertEqual(mapping.new_to_old(0), 0)
        self.assertEqual(mapping.new_to_old(1), 1)
        for added_rank in (2, 3):
            result = mapping.locate_new(added_rank)
            self.assertTrue(result.added)
            with self.assertRaises(AddedRankError):
                mapping.new_to_old(added_rank)

    def test_every_rank_in_deterministic_small_mapping_reconciles(self):
        db = RankIndex(
            R=[r_row("r1", 1, 2, 2), r_row("r2", 1, 2, 1)],
            S=[s_row("s1", 2, 3, 2), s_row("s2", 2, 3, 2)],
            T=[t_row("t1", 3, 1, 3)],
        )
        old = db.current
        with db.transaction() as tx:
            tx.set_multiplicity("R", "r1", 3)
            tx.set_multiplicity("S", "s1", 1)
            tx.set_multiplicity("T", "t1", 2)
            tx.insert(r_row("r3", 1, 2, 1))
        mapping = db.last_mapping
        new = db.current

        self.assertEqual(
            mapping.deleted_old_count + mapping.retained_count, old.total
        )
        self.assertEqual(mapping.added_new_count + mapping.retained_count, new.total)

        for rank in range(old.total):
            located = mapping.locate_old(rank)
            if located.retained:
                self.assertEqual(mapping.old_to_new(rank), located.new_rank)
                self.assertEqual(new.at(located.new_rank), old.at(rank))
            else:
                interval = mapping.deleted_interval_containing(rank)
                self.assertLessEqual(interval[0], rank)
                self.assertLess(rank, interval[1])

        for rank in range(new.total):
            located = mapping.locate_new(rank)
            if located.retained:
                self.assertEqual(mapping.new_to_old(rank), located.old_rank)
                self.assertEqual(old.at(located.old_rank), new.at(rank))
            else:
                interval = mapping.added_interval_containing(rank)
                self.assertLessEqual(interval[0], rank)
                self.assertLess(rank, interval[1])

        retained_copy = old.at(0)
        mapped_new_copy = mapping.map_copy_to_new(retained_copy)
        self.assertEqual(mapped_new_copy, new.at(mapping.old_to_new(0)))
        self.assertEqual(
            mapping.map_copy_to_old(mapped_new_copy), retained_copy
        )

    def test_mapping_construction_and_locate_do_not_enumerate_blocks(self):
        db = RankIndex(
            R=[r_row("r", 1, 2, 2)],
            S=[s_row("s", 2, 3, 1)],
            T=[t_row("t", 3, 1, 1)],
        )
        # Commit and locate while block enumeration is forbidden.  Rank and
        # coordinate formulas are sufficient for construction and lookup.
        with mock.patch.object(
            KeyGroup, "iter_blocks", side_effect=AssertionError("eager block expansion")
        ):
            mapping = db.set_multiplicity("R", "r", 1)
            self.assertEqual(mapping.deleted_old_count, 1)
            result = mapping.locate_old(1)
            self.assertTrue(result.deleted)
            self.assertEqual(mapping.deleted_interval_containing(1), (1, 2))

        # Explicit ordinary interval expansion remains available and correct.
        self.assertEqual(mapping.deleted_old_ranges.merged_intervals(), ((1, 2),))


class IncrementalKeyGroupTest(unittest.TestCase):
    @staticmethod
    def _one_row_key(i):
        key = (i, i, i)
        return (
            r_row(f"r{i}", i, i, 1),
            s_row(f"s{i}", i, i, 1),
            t_row(f"t{i}", i, i, 1),
        )

    def test_update_rebuilds_only_affected_key_group(self):
        rows = [self._one_row_key(i) for i in range(3)]
        db = RankIndex(
            R=[x[0] for x in rows],
            S=[x[1] for x in rows],
            T=[x[2] for x in rows],
        )
        old = db.current
        affected_key = (1, 1, 1)
        calls = []

        def spy(key, tables):
            calls.append(key)
            return real_build_group(key, tables)

        with mock.patch("rank_index.snapshot.build_group", side_effect=spy):
            mapping = db.set_multiplicity("R", "r1", 2)

        self.assertEqual(calls, [affected_key])
        new = db.current
        self.assertIs(new.group((0, 0, 0)), old.group((0, 0, 0)))
        self.assertIs(new.group((2, 2, 2)), old.group((2, 2, 2)))
        self.assertIsNot(new.group(affected_key), old.group(affected_key))

        self.assertEqual(len(mapping.unaffected_segments), 2)
        self.assertEqual(mapping.old_to_new(2), 3)
        self.assertEqual(mapping.new_to_old(3), 2)
        self.assertEqual(mapping.added_new_ranges.merged_intervals(), ((2, 3),))


class AtomicTriangleTransactionTest(unittest.TestCase):
    def test_simultaneous_triangle_insert_shrink_and_full_old_cursor(self):
        db = RankIndex(
            R=[r_row("old-r", 0, 0, 5), r_row("doomed-r", 1, 1, 1)],
            S=[s_row("old-s", 0, 0, 5), s_row("doomed-s", 1, 1, 1)],
            T=[t_row("old-t", 0, 0, 5), t_row("doomed-t", 1, 1, 1)],
        )
        old = db.current
        old_cursor = old.cursor()
        self.assertEqual(old.total, 126)  # 125 + doomed triangle

        calls = []

        def spy(key, tables):
            calls.append(key)
            return real_build_group(key, tables)

        with mock.patch("rank_index.snapshot.build_group", side_effect=spy):
            with db.transaction() as tx:
                tx.set_multiplicity("R", "old-r", 3)
                tx.delete("S", "doomed-s")
                tx.insert(r_row("new-r", 10, 20, 2))
                tx.insert(s_row("new-s", 20, 30, 4))
                tx.insert(t_row("new-t", 30, 10, 5))

        mapping = db.last_mapping
        new = db.current

        # Old K0 retained 3*5*5=75/deleted50; K1 deleted1; new K2=2*4*5=40.
        self.assertEqual(new.total, 115)
        self.assertEqual(mapping.retained_count, 75)
        self.assertEqual(mapping.deleted_old_count, 51)
        self.assertEqual(mapping.added_new_count, 40)
        self.assertEqual(mapping.added_new_ranges.merged_intervals(), ((75, 115),))
        # The shrunk K0 suffix [75,125) and deleted K1 group [125,126) are
        # adjacent and therefore compress into one maximal interval.
        self.assertEqual(
            mapping.deleted_old_ranges.merged_intervals(), ((75, 126),)
        )

        # The simultaneously inserted triangle is built once as one group.
        new_key = (10, 20, 30)
        self.assertEqual(calls.count(new_key), 1)
        self.assertEqual(new.group(new_key).total, 40)

        # Pinned old cursor still reads the complete old rank space.
        read = list(old_cursor)
        self.assertEqual(len(read), 126)
        self.assertEqual([old.rank_of(c) for c in read], list(range(126)))
        self.assertEqual(old_cursor.remaining, 0)

        # New cursor can read the complete new space too.
        new_cursor = new.cursor()
        new_read = new_cursor.fetch_many(1_000)
        self.assertEqual(len(new_read), 115)
        self.assertEqual([new.rank_of(c) for c in new_read], list(range(115)))

    def test_exception_rolls_back_entire_transaction(self):
        db = triangle_db(3)
        before = db.current
        with self.assertRaises(RuntimeError):
            with db.transaction() as tx:
                tx.set_multiplicity("R", "r", 2)
                tx.insert(r_row("r2", 9, 9, 1))
                raise RuntimeError("force rollback")
        after = db.current
        self.assertEqual(after.version, before.version)
        self.assertEqual(after.total, 27)
        self.assertIsNone(after.group((9, 9, 9)))
        self.assertEqual(after.group((1, 2, 3)).r_m, (3,))

    def test_explicit_rollback_inside_context_is_allowed(self):
        db = triangle_db(3)
        with db.transaction() as tx:
            tx.set_multiplicity("R", "r", 1)
            tx.rollback()
        self.assertEqual(db.current.total, 27)
        self.assertEqual(db.current.version, 0)

    def test_optimistic_concurrent_transaction_conflict_is_explicit(self):
        db = triangle_db(2)
        t1 = db.transaction()
        t2 = db.transaction()
        t1.set_multiplicity("R", "r", 3).commit()
        t2.set_multiplicity("S", "s", 4)
        with self.assertRaises(ConcurrentUpdateError):
            t2.commit()
        t2.rollback()

        # A replacement transaction based on the latest version can retry.
        with db.transaction() as t3:
            t3.set_multiplicity("S", "s", 4)

        group = db.current.group((1, 2, 3))
        self.assertEqual(group.r_m, (3,))
        self.assertEqual(group.s_m, (4,))


if __name__ == "__main__":
    unittest.main()
