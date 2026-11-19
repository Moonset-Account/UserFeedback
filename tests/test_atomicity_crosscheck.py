"""Cross-cutting acceptance test: naive three-step swap leaks a temp name.

This is the negative control for the atomic-exchange requirement.  The same
final arrangement can be reached by three ordinary renames through a
scratch name; doing so is *observable* at an intermediate sequence.  The
single ``exchange`` event is not.  If the index ever regressed to modelling
exchange as renames, this test would catch it.
"""

from __future__ import annotations

import unittest

from support import exchange, link, make_index, rename

from naming_history import AlreadyExistsError, InvalidEventError


class TemporaryNameIsEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.initial = [
            {"dir": "root", "name": "a", "id": "fa"},
            {"dir": "root", "name": "b", "id": "fb"},
        ]

    def test_three_step_swap_exposes_a_scratch_path(self):
        index = make_index(self.initial)
        index.commit(rename("root", "a", "root", ".swap-tmp"))
        index.commit(rename("root", "b", "root", "a"))
        index.commit(rename("root", ".swap-tmp", "root", "b"))

        self.assertEqual(["/.swap-tmp"], index.paths_at(1, "fa"))
        self.assertEqual(["/.swap-tmp"], index.paths_at(2, "fa"))
        self.assertEqual(["/b"], index.paths_at(3, "fa"))

    def test_atomic_exchange_never_exposes_that_scratch_path(self):
        index = make_index(self.initial)
        index.commit(exchange("root", "a", "root", "b"))

        for seq in range(index.last_seq + 1):
            for paths in index.all_paths_at(seq).values():
                self.assertNotIn("/.swap-tmp", paths)
        self.assertEqual(1, index.committed_events)


class CrossConstraintTest(unittest.TestCase):
    """Link, directory rename and exchange combined in one timeline."""

    def test_full_timeline_matches_hand_computed_paths(self):
        index = make_index(
            [
                {"dir": "root", "name": "work", "id": "d_work"},
                {"dir": "d_work", "name": "note.txt", "id": "f_note"},
                {"dir": "root", "name": "backup", "id": "d_bak"},
                {"dir": "d_bak", "name": "old.txt", "id": "f_old"},
            ],
            objects={"root": "dir", "d_work": "dir", "d_bak": "dir"},
        )

        # 1: hard-link the old file up to the root.
        index.commit(link("root", "legacy.txt", "f_old"))
        # 2: rename the work directory; paths under it must follow.
        index.commit(rename("root", "work", "root", "current"))
        # 3: atomically swap the two top-level directory names.
        index.commit(exchange("root", "current", "root", "backup"))

        self.assertEqual(["/work/note.txt"], index.paths_at(0, "f_note"))
        self.assertEqual(["/backup/old.txt"], index.paths_at(0, "f_old"))

        self.assertEqual(["/current/note.txt"], index.paths_at(2, "f_note"))
        self.assertEqual(
            ["/backup/old.txt", "/legacy.txt"], index.paths_at(2, "f_old")
        )

        # After the swap the directories exchanged their prefixes.
        self.assertEqual(["/backup/note.txt"], index.paths_at(3, "f_note"))
        self.assertEqual(
            ["/current/old.txt", "/legacy.txt"], index.paths_at(3, "f_old")
        )
        self.assertEqual(["/backup"], index.paths_at(3, "d_work"))
        self.assertEqual(["/current"], index.paths_at(3, "d_bak"))

    def test_rejected_event_mid_timeline_does_not_shift_later_sequences(self):
        index = make_index(
            [
                {"dir": "root", "name": "a", "id": "fa"},
                {"dir": "root", "name": "b", "id": "fb"},
            ]
        )
        index.commit(exchange("root", "a", "root", "b"))
        with self.assertRaises(AlreadyExistsError):
            index.commit(link("root", "a", "f_new"))

        self.assertEqual(1, index.committed_events)
        self.assertEqual(["/a"], index.paths_at(1, "fb"))
        self.assertEqual(["/b"], index.paths_at(1, "fa"))

    def test_failed_commit_leaves_no_partial_state(self):
        index = make_index(
            [
                {"dir": "root", "name": "d", "id": "dd"},
                {"dir": "dd", "name": "x", "id": "fx"},
                {"dir": "root", "name": "f", "id": "fy"},
            ],
            objects={"root": "dir", "dd": "dir"},
        )
        before = index.all_paths_at(0)
        with self.assertRaises(InvalidEventError):
            # Replacing a non-empty directory with a file is not allowed.
            index.commit(rename("root", "f", "root", "d"))

        self.assertEqual(0, index.committed_events)
        self.assertEqual(before, index.all_paths_at(0))
        self.assertEqual(["/d/x"], index.paths_at(0, "fx"))
        self.assertEqual(["/f"], index.paths_at(0, "fy"))


if __name__ == "__main__":
    unittest.main()
