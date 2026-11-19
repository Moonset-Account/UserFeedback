"""Acceptance tests: rename-over-existing-entry rules.

``rename`` is the one operation that can make a name disappear without an
explicit unlink.  These tests pin down which replacements are allowed, and
that a rejected replacement neither commits nor corrupts earlier answers.
"""

from __future__ import annotations

import unittest

from support import make_index, rename

from naming_history import GoneObjectError, InvalidEventError


class RenameReplacementTest(unittest.TestCase):
    def test_file_can_replace_file(self):
        index = make_index(
            [
                {"dir": "root", "name": "src", "id": "f_new"},
                {"dir": "root", "name": "dst", "id": "f_old"},
            ]
        )
        index.commit(rename("root", "src", "root", "dst"))
        self.assertEqual(["/dst"], index.paths_at(1, "f_new"))
        with self.assertRaises(GoneObjectError):
            index.paths_at(1, "f_old")
        # History is still intact at the earlier sequence.
        self.assertEqual(["/dst"], index.paths_at(0, "f_old"))

    def test_directory_can_replace_empty_directory(self):
        index = make_index(
            [
                {"dir": "root", "name": "src", "id": "d_new"},
                {"dir": "d_new", "name": "n", "id": "f_n"},
                {"dir": "root", "name": "dst", "id": "d_old"},
            ],
            objects={"root": "dir", "d_new": "dir", "d_old": "dir"},
        )
        index.commit(rename("root", "src", "root", "dst"))
        self.assertEqual(["/dst/n"], index.paths_at(1, "f_n"))
        self.assertEqual(["/dst"], index.paths_at(1, "d_new"))
        with self.assertRaises(GoneObjectError):
            index.paths_at(1, "d_old")

    def test_directory_cannot_replace_non_empty_directory(self):
        index = make_index(
            [
                {"dir": "root", "name": "src", "id": "d_new"},
                {"dir": "root", "name": "dst", "id": "d_old"},
                {"dir": "d_old", "name": "keep", "id": "f_keep"},
            ],
            objects={"root": "dir", "d_new": "dir", "d_old": "dir"},
        )
        with self.assertRaises(InvalidEventError):
            index.commit(rename("root", "src", "root", "dst"))
        self.assertEqual(0, index.committed_events)
        self.assertEqual(["/dst/keep"], index.paths_at(0, "f_keep"))

    def test_directory_cannot_replace_file(self):
        index = make_index(
            [
                {"dir": "root", "name": "src", "id": "d_new"},
                {"dir": "root", "name": "dst", "id": "f_old"},
            ],
            objects={"root": "dir", "d_new": "dir"},
        )
        with self.assertRaises(InvalidEventError):
            index.commit(rename("root", "src", "root", "dst"))
        self.assertEqual(0, index.committed_events)

    def test_file_cannot_replace_directory(self):
        index = make_index(
            [
                {"dir": "root", "name": "src", "id": "f_new"},
                {"dir": "root", "name": "dst", "id": "d_old"},
            ],
            objects={"root": "dir", "d_old": "dir"},
        )
        with self.assertRaises(InvalidEventError):
            index.commit(rename("root", "src", "root", "dst"))
        self.assertEqual(0, index.committed_events)

    def test_rename_onto_itself_is_a_no_op(self):
        index = make_index([{"dir": "root", "name": "a", "id": "fa"}])
        index.commit(rename("root", "a", "root", "a"))
        self.assertEqual(["/a"], index.paths_at(1, "fa"))

    def test_rename_missing_source_is_rejected(self):
        from naming_history import NotFoundError

        index = make_index([{"dir": "root", "name": "a", "id": "fa"}])
        with self.assertRaises(NotFoundError):
            index.commit(rename("root", "nope", "root", "b"))
        self.assertEqual(0, index.committed_events)


if __name__ == "__main__":
    unittest.main()
