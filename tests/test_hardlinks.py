"""Acceptance tests: hard links and sequence-accurate path queries."""

from __future__ import annotations

import unittest

from support import link, make_index, rename, unlink

from naming_history import AlreadyExistsError, UnknownObjectError


class MultipleHardLinksTest(unittest.TestCase):
    def test_file_with_several_links_reports_every_path(self):
        index = make_index(
            [
                {"dir": "root", "name": "a", "id": "f1"},
                {"dir": "root", "name": "b", "id": "f1"},
            ]
        )
        self.assertEqual(["/a", "/b"], index.paths_at(0, "f1"))

    def test_new_link_becomes_visible_only_after_its_event(self):
        index = make_index([{"dir": "root", "name": "a", "id": "f1"}])
        index.commit(link("root", "b", "f1"))

        self.assertEqual(["/a"], index.paths_at(0, "f1"))
        self.assertEqual(["/a", "/b"], index.paths_at(1, "f1"))

    def test_removing_one_link_keeps_the_others(self):
        index = make_index(
            [
                {"dir": "root", "name": "a", "id": "f1"},
                {"dir": "root", "name": "b", "id": "f1"},
            ]
        )
        index.commit(unlink("root", "a"))

        self.assertEqual(["/a", "/b"], index.paths_at(0, "f1"))
        self.assertEqual(["/b"], index.paths_at(1, "f1"))

    def test_link_into_another_directory_prefixes_correctly(self):
        index = make_index(
            [
                {"dir": "root", "name": "a", "id": "f1"},
                {"dir": "root", "name": "d", "id": "d1"},
            ],
            objects={"root": "dir", "d1": "dir"},
        )
        index.commit(link("d1", "inner", "f1"))
        self.assertEqual(["/a", "/d/inner"], index.paths_at(1, "f1"))

        index.commit(rename("root", "d", "root", "e"))
        self.assertEqual(["/a", "/e/inner"], index.paths_at(2, "f1"))


class HistoryIsAppendOnlyTest(unittest.TestCase):
    def test_earlier_answers_are_stable_after_later_events(self):
        index = make_index([{"dir": "root", "name": "a", "id": "f1"}])
        before = index.paths_at(0, "f1")
        index.commit(rename("root", "a", "root", "z"))
        index.commit(link("root", "y", "f1"))

        self.assertEqual(["/a"], before)
        self.assertEqual(["/a"], index.paths_at(0, "f1"))
        self.assertEqual(["/z"], index.paths_at(1, "f1"))
        self.assertEqual(["/y", "/z"], index.paths_at(2, "f1"))


class DuplicateNamesAreRejectedTest(unittest.TestCase):
    def test_link_onto_existing_name_is_refused_and_not_committed(self):
        index = make_index([{"dir": "root", "name": "a", "id": "f1"}])
        with self.assertRaises(AlreadyExistsError):
            index.commit(link("root", "a", "f2"))

        self.assertEqual(0, index.committed_events)
        self.assertEqual(["/a"], index.paths_at(0, "f1"))
        with self.assertRaises(UnknownObjectError):
            index.paths_at(0, "f2")


if __name__ == "__main__":
    unittest.main()
