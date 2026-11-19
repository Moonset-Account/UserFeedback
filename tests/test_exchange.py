"""Acceptance tests: atomic exchange never exposes a temporary path.

The requirement is that swapping two names in the same parent must be a
single committed transition.  A naive implementation would rename ``a`` to
a scratch name, rename ``b`` to ``a``, then rename the scratch name to
``b``; the scratch name would be visible at an intermediate sequence and
could be reported as evidence.  Here there is exactly one sequence number
for the swap, and no scratch name exists in any snapshot.
"""

from __future__ import annotations

import unittest

from support import exchange, make_index

from naming_history import InvalidEventError, NotFoundError


class AtomicExchangeTest(unittest.TestCase):
    def setUp(self):
        self.index = make_index(
            [
                {"dir": "root", "name": "a", "id": "fa"},
                {"dir": "root", "name": "b", "id": "fb"},
            ]
        )

    def test_swap_takes_effect_in_one_sequence_step(self):
        self.index.commit(exchange("root", "a", "root", "b"))

        self.assertEqual(1, self.index.committed_events)
        self.assertEqual(["/a"], self.index.paths_at(0, "fa"))
        self.assertEqual(["/b"], self.index.paths_at(0, "fb"))
        self.assertEqual(["/b"], self.index.paths_at(1, "fa"))
        self.assertEqual(["/a"], self.index.paths_at(1, "fb"))

    def test_no_temporary_name_is_queryable(self):
        self.index.commit(exchange("root", "a", "root", "b"))
        queryable = set()
        for seq in range(self.index.last_seq + 1):
            for paths in self.index.all_paths_at(seq).values():
                queryable.update(paths)
        self.assertEqual({"/", "/a", "/b"}, queryable)
        for path in queryable:
            self.assertFalse(
                path.lstrip("/").startswith("."),
                "swap leaked a temporary name: %s" % path,
            )

    def test_two_objects_still_exist_after_the_swap(self):
        self.index.commit(exchange("root", "a", "root", "b"))
        snapshot = self.index.all_paths_at(1)
        self.assertEqual(["/b"], snapshot["fa"])
        self.assertEqual(["/a"], snapshot["fb"])

    def test_exchange_rejects_a_missing_side_and_commits_nothing(self):
        with self.assertRaises(NotFoundError):
            self.index.commit(exchange("root", "a", "root", "missing"))
        self.assertEqual(0, self.index.committed_events)
        self.assertEqual(["/a"], self.index.paths_at(0, "fa"))
        self.assertEqual(["/b"], self.index.paths_at(0, "fb"))

    def test_exchange_of_directories_carries_their_subtrees(self):
        index = make_index(
            [
                {"dir": "root", "name": "x", "id": "dx"},
                {"dir": "dx", "name": "one", "id": "f1"},
                {"dir": "root", "name": "y", "id": "dy"},
                {"dir": "dy", "name": "two", "id": "f2"},
            ],
            objects={"root": "dir", "dx": "dir", "dy": "dir"},
        )
        index.commit(exchange("root", "x", "root", "y"))
        self.assertEqual(["/y/one"], index.paths_at(1, "f1"))
        self.assertEqual(["/x/two"], index.paths_at(1, "f2"))
        self.assertEqual(["/x/one"], index.paths_at(0, "f1"))

    def test_exchange_into_own_subtree_is_rejected(self):
        index = make_index(
            [
                {"dir": "root", "name": "d", "id": "dd"},
                {"dir": "dd", "name": "inner", "id": "di"},
                {"dir": "di", "name": "leaf", "id": "fl"},
            ],
            objects={"root": "dir", "dd": "dir", "di": "dir"},
        )
        # Swapping directory d with a directory that lives inside d would
        # make d its own descendant.
        with self.assertRaises(InvalidEventError):
            index.commit(exchange("root", "d", "dd", "inner"))
        self.assertEqual(0, index.committed_events)
        self.assertEqual(["/d/inner/leaf"], index.paths_at(0, "fl"))

    def test_exchange_of_a_name_with_itself_is_a_no_op(self):
        self.index.commit(exchange("root", "a", "root", "a"))
        self.assertEqual(["/a"], self.index.paths_at(1, "fa"))


if __name__ == "__main__":
    unittest.main()
