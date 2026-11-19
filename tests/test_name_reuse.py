"""Acceptance tests: later objects must not leak into earlier answers.

This is the "don't count a same-named object that only appeared later"
constraint.  An investigator who reads sequence 2 must see exactly the
namespace of event 2, even when event 5 later reuses the name.
"""

from __future__ import annotations

import unittest

from support import link, make_index, rename, unlink

from naming_history import FutureObjectError, GoneObjectError


class NameReuseTest(unittest.TestCase):
    def test_reused_name_points_at_a_different_object_per_sequence(self):
        index = make_index([{"dir": "root", "name": "report", "id": "v1"}])
        index.commit(unlink("root", "report"))
        index.commit(link("root", "report", "v2"))

        self.assertEqual(["/report"], index.paths_at(0, "v1"))
        self.assertEqual(["/report"], index.paths_at(2, "v2"))
        # At sequence 2 viewer of the old object must not see the new path.
        with self.assertRaises(GoneObjectError):
            index.paths_at(2, "v1")

    def test_object_introduced_later_is_invisible_earlier(self):
        index = make_index([{"dir": "root", "name": "a", "id": "f1"}])
        index.commit(link("root", "b", "f2"))
        with self.assertRaises(FutureObjectError):
            index.paths_at(0, "f2")
        self.assertEqual(["/b"], index.paths_at(1, "f2"))

    def test_unknown_identifier_is_rejected_everywhere(self):
        index = make_index([{"dir": "root", "name": "a", "id": "f1"}])
        from naming_history import UnknownObjectError

        with self.assertRaises(UnknownObjectError):
            index.paths_at(0, "never")

    def test_retired_identifier_cannot_be_reused_for_a_new_object(self):
        from naming_history import InvalidEventError

        index = make_index([{"dir": "root", "name": "a", "id": "f1"}])
        index.commit(unlink("root", "a"))
        with self.assertRaises(InvalidEventError):
            index.commit(link("root", "a", "f1"))
        self.assertEqual(1, index.committed_events)

    def test_same_name_in_different_directories_stays_distinct(self):
        index = make_index(
            [
                {"dir": "root", "name": "d1", "id": "d1"},
                {"dir": "root", "name": "d2", "id": "d2"},
                {"dir": "d1", "name": "x", "id": "o1"},
                {"dir": "d2", "name": "x", "id": "o2"},
            ],
            objects={"root": "dir", "d1": "dir", "d2": "dir"},
        )
        index.commit(rename("root", "d1", "root", "moved"))
        self.assertEqual(["/d2/x"], index.paths_at(1, "o2"))
        self.assertEqual(["/moved/x"], index.paths_at(1, "o1"))
        self.assertEqual(["/d1/x"], index.paths_at(0, "o1"))


class SequenceBoundsTest(unittest.TestCase):
    def test_out_of_range_sequence_is_rejected(self):
        from naming_history import InvalidQueryError

        index = make_index([{"dir": "root", "name": "a", "id": "f1"}])
        with self.assertRaises(InvalidQueryError):
            index.paths_at(1, "f1")
        with self.assertRaises(InvalidQueryError):
            index.paths_at(-1, "f1")

    def test_sequence_zero_is_the_initial_tree(self):
        index = make_index([{"dir": "root", "name": "a", "id": "f1"}])
        self.assertEqual(0, index.last_seq)
        self.assertEqual(["/a"], index.paths_at(0, "f1"))


if __name__ == "__main__":
    unittest.main()
