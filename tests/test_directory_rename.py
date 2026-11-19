"""Acceptance tests: directory renames move the visible subtree prefix."""

from __future__ import annotations

import unittest

from support import link, make_index, rename, unlink

from naming_history import GoneObjectError, InvalidEventError


class SubtreeRenameTest(unittest.TestCase):
    def setUp(self):
        self.index = make_index(
            [
                {"dir": "root", "name": "proj", "id": "d_proj"},
                {"dir": "d_proj", "name": "src", "id": "d_src"},
                {"dir": "d_src", "name": "main.c", "id": "f_main"},
                {"dir": "d_src", "name": "util.c", "id": "f_util"},
                {"dir": "root", "name": "dest", "id": "d_dest"},
                {"dir": "root", "name": "empty", "id": "d_empty"},
            ],
            objects={
                "root": "dir",
                "d_proj": "dir",
                "d_src": "dir",
                "d_dest": "dir",
                "d_empty": "dir",
            },
        )

    def test_rename_updates_every_descendant_prefix(self):
        self.assertEqual(["/proj/src/main.c"], self.index.paths_at(0, "f_main"))

        self.index.commit(rename("root", "proj", "root", "archive"))

        self.assertEqual(["/archive/src/main.c"], self.index.paths_at(1, "f_main"))
        self.assertEqual(["/archive/src/util.c"], self.index.paths_at(1, "f_util"))
        self.assertEqual(["/archive/src"], self.index.paths_at(1, "d_src"))

        # The earlier snapshot is untouched: this is the forensic property.
        self.assertEqual(["/proj/src/main.c"], self.index.paths_at(0, "f_main"))

    def test_rename_directory_into_another_directory(self):
        self.index.commit(rename("d_proj", "src", "d_dest", "src"))
        self.assertEqual(["/dest/src/main.c"], self.index.paths_at(1, "f_main"))
        self.assertEqual(["/proj"], self.index.paths_at(1, "d_proj"))
        self.assertEqual(["/proj/src"], self.index.paths_at(0, "d_src"))

    def test_two_successive_directory_renames_compose(self):
        self.index.commit(rename("root", "proj", "root", "p2"))
        self.index.commit(rename("root", "p2", "root", "p3"))
        self.assertEqual(["/p3/src/main.c"], self.index.paths_at(2, "f_main"))
        self.assertEqual(["/p2/src/main.c"], self.index.paths_at(1, "f_main"))

    def test_same_filename_in_different_directories_is_not_confused(self):
        self.index.commit(link("root", "main.c", "f_other"))
        self.assertEqual(["/main.c"], self.index.paths_at(1, "f_other"))
        self.assertEqual(["/proj/src/main.c"], self.index.paths_at(1, "f_main"))


class DirectoryInvariantTest(unittest.TestCase):
    def setUp(self):
        self.index = make_index(
            [
                {"dir": "root", "name": "proj", "id": "d_proj"},
                {"dir": "d_proj", "name": "src", "id": "d_src"},
                {"dir": "d_src", "name": "main.c", "id": "f_main"},
                {"dir": "root", "name": "empty", "id": "d_empty"},
            ],
            objects={
                "root": "dir",
                "d_proj": "dir",
                "d_src": "dir",
                "d_empty": "dir",
            },
        )

    def test_moving_a_directory_into_its_own_subtree_is_rejected(self):
        with self.assertRaises(InvalidEventError):
            self.index.commit(rename("root", "proj", "d_src", "loop"))

        self.assertEqual(0, self.index.committed_events)
        self.assertEqual(["/proj/src"], self.index.paths_at(0, "d_src"))

    def test_unlinking_a_non_empty_directory_is_rejected(self):
        with self.assertRaises(InvalidEventError):
            self.index.commit(unlink("root", "proj"))
        self.assertEqual(0, self.index.committed_events)

    def test_directory_cannot_be_hard_linked_to_second_parent(self):
        with self.assertRaises(InvalidEventError):
            self.index.commit(link("root", "alias", "d_src"))
        self.assertEqual(0, self.index.committed_events)
        self.assertEqual(["/proj/src"], self.index.paths_at(0, "d_src"))

    def test_removing_an_empty_directory_drops_its_object(self):
        self.assertEqual(["/empty"], self.index.paths_at(0, "d_empty"))
        self.index.commit(unlink("root", "empty"))
        with self.assertRaises(GoneObjectError):
            self.index.paths_at(1, "d_empty")


if __name__ == "__main__":
    unittest.main()
