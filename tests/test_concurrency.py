import pathlib
import sys
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from rank_index import RankIndex, r_row, s_row, t_row  # noqa: E402


class ConcurrentSnapshotTest(unittest.TestCase):
    def test_readers_keep_old_rank_space_while_writer_commits(self):
        db = RankIndex(
            R=[r_row("r", 1, 2, 8)],
            S=[s_row("s", 2, 3, 8)],
            T=[t_row("t", 3, 1, 8)],
        )
        fixed = db.current
        pinned_cursor = fixed.cursor(0)
        self.assertEqual(fixed.total, 512)

        stop = threading.Event()
        errors: list[BaseException] = []

        def reader(seed: int):
            try:
                i = seed
                local_cursor = fixed.cursor(seed % fixed.total)
                while not stop.is_set():
                    rank = i % fixed.total
                    copy = fixed.at(rank)
                    if fixed.rank_of(copy) != rank:
                        raise AssertionError("reverse rank mismatch")
                    if i % 17 == 0:
                        if local_cursor.remaining == 0:
                            local_cursor.seek(0)
                        next(local_cursor)
                    i += 1
            except BaseException as exc:  # report thread failure to main test
                errors.append(exc)

        threads = [threading.Thread(target=reader, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()

        try:
            # Shrink and regrow one physical row through many committed
            # versions.  Readers must remain in version zero's rank space.
            for multiplicity in [7, 6, 5, 4, 3, 2, 1, 2, 4, 8]:
                db.set_multiplicity("R", "r", multiplicity)
            newest = db.current
            self.assertEqual(newest.total, 512)
            self.assertGreater(newest.version, fixed.version)
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(fixed.total, 512)

        # A cursor fixed before the writes still scans every old copy.
        copies = list(pinned_cursor)
        self.assertEqual(len(copies), 512)
        self.assertEqual(
            [fixed.rank_of(copy) for copy in copies], list(range(512))
        )

    def test_interleaved_old_and_new_cursor_reads(self):
        db = RankIndex(
            R=[r_row("r", 1, 2, 2)],
            S=[s_row("s", 2, 3, 2)],
            T=[t_row("t", 3, 1, 2)],
        )
        old = db.current
        old_cursor = old.cursor()
        mapping = db.set_multiplicity("T", "t", 3)
        new = db.current
        new_cursor = new.cursor()

        old_seen = []
        new_seen = []
        while old_cursor.remaining or new_cursor.remaining:
            if old_cursor.remaining:
                old_seen.append(next(old_cursor))
            if new_cursor.remaining:
                new_seen.append(next(new_cursor))

        self.assertEqual(len(old_seen), old.total)
        self.assertEqual(len(new_seen), new.total)
        for old_copy in old_seen:
            old_rank = old.rank_of(old_copy)
            result = mapping.locate_old(old_rank)
            if result.retained:
                self.assertEqual(new.at(result.new_rank), old_copy)


if __name__ == "__main__":
    unittest.main()
