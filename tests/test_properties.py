import pathlib
import random
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from rank_index import AddedRankError, DeletedRankError, RankIndex, r_row, s_row, t_row  # noqa: E402
from rank_index import treap  # noqa: E402


class RandomSmallSpacePropertyTest(unittest.TestCase):
    def test_rank_roundtrip_and_mapping_for_many_deterministic_cases(self):
        rng = random.Random(20261124)
        for case in range(120):
            nr = rng.randint(1, 4)
            ns = rng.randint(1, 4)
            nt = rng.randint(1, 4)
            R = [
                r_row(f"r{i}", rng.randrange(3), rng.randrange(3), rng.randint(1, 5))
                for i in range(nr)
            ]
            S = [
                s_row(f"s{i}", rng.randrange(3), rng.randrange(3), rng.randint(1, 5))
                for i in range(ns)
            ]
            T = [
                t_row(f"t{i}", rng.randrange(3), rng.randrange(3), rng.randint(1, 5))
                for i in range(nt)
            ]
            db = RankIndex(R=R, S=S, T=T)
            old = db.current
            treap.validate(old.root)

            for rank in range(old.total):
                copy = old.at(rank)
                self.assertEqual(old.rank_of(copy), rank, case)

            with db.transaction() as tx:
                for table, n, prefix in (("R", nr, "r"), ("S", ns, "s"), ("T", nt, "t")):
                    for i in range(n):
                        if rng.random() < 0.35:
                            tx.set_multiplicity(
                                table, f"{prefix}{i}", rng.randint(1, 5)
                            )
                if rng.random() < 0.15 and nr > 1:
                    tx.delete("R", "r0")
                if rng.random() < 0.15:
                    tx.insert(
                        r_row("rx", rng.randrange(3), rng.randrange(3), rng.randint(1, 4))
                    )

            mapping = db.last_mapping
            new = db.current
            treap.validate(new.root)
            self.assertEqual(
                mapping.retained_count + mapping.deleted_old_count, old.total
            )
            self.assertEqual(
                mapping.retained_count + mapping.added_new_count, new.total
            )

            for rank in range(old.total):
                located = mapping.locate_old(rank)
                if located.retained:
                    self.assertEqual(mapping.old_to_new(rank), located.new_rank)
                    self.assertEqual(new.at(located.new_rank), old.at(rank))
                else:
                    with self.assertRaises(DeletedRankError):
                        mapping.old_to_new(rank)
                    interval = mapping.deleted_interval_containing(rank)
                    self.assertLessEqual(interval[0], rank)
                    self.assertLess(rank, interval[1])

            for rank in range(new.total):
                located = mapping.locate_new(rank)
                if located.retained:
                    self.assertEqual(mapping.new_to_old(rank), located.old_rank)
                else:
                    with self.assertRaises(AddedRankError):
                        mapping.new_to_old(rank)
                    interval = mapping.added_interval_containing(rank)
                    self.assertLessEqual(interval[0], rank)
                    self.assertLess(rank, interval[1])


if __name__ == "__main__":
    unittest.main()
