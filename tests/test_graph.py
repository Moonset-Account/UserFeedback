"""Acceptance tests: DAG construction, readiness and dependency kinds."""

from __future__ import annotations

import unittest

from shufflesched import (
    DuplicatePartition,
    ShuffleScheduler,
    UnknownDependency,
    UnknownPartition,
)

from .helpers import diamond_scheduler, wide_diamond_scheduler


class DagConstructionTests(unittest.TestCase):
    def test_source_partition_has_no_upstreams(self):
        sched = ShuffleScheduler()
        part = sched.add_partition("S")
        self.assertEqual(part.kind, "source")
        self.assertEqual(part.upstreams, ())

    def test_kind_is_inferred_from_upstreams(self):
        sched = ShuffleScheduler()
        sched.add_partition("S")
        narrow = sched.add_partition("N", ["S"])
        shuffle = sched.add_partition("R", ["S"], kind="shuffle")
        self.assertEqual(narrow.kind, "shuffle")  # default when upstreams exist
        self.assertEqual(shuffle.kind, "shuffle")

    def test_explicit_narrow_kind_is_accepted(self):
        sched = ShuffleScheduler()
        sched.add_partition("S")
        narrow = sched.add_partition("M", ["S"], kind="narrow")
        self.assertEqual(narrow.kind, "narrow")

    def test_duplicate_partition_is_rejected(self):
        sched = ShuffleScheduler()
        sched.add_partition("S")
        with self.assertRaises(DuplicatePartition):
            sched.add_partition("S")

    def test_source_with_upstreams_is_rejected(self):
        sched = ShuffleScheduler()
        sched.add_partition("S")
        with self.assertRaises(ValueError):
            sched.add_partition("X", ["S"], kind="source")

    def test_non_source_without_upstreams_is_rejected(self):
        sched = ShuffleScheduler()
        with self.assertRaises(ValueError):
            sched.add_partition("X", kind="narrow")

    def test_unknown_dependency_is_rejected_at_seal(self):
        sched = ShuffleScheduler()
        sched.add_partition("R", ["missing"], kind="shuffle")
        with self.assertRaises(UnknownDependency):
            sched.seal()

    def test_cycle_is_rejected_at_seal(self):
        sched = ShuffleScheduler()
        sched.add_partition("A", ["B"], kind="narrow")
        sched.add_partition("B", ["A"], kind="narrow")
        with self.assertRaises(ValueError):
            sched.seal()

    def test_unknown_partition_lookup(self):
        sched = ShuffleScheduler()
        with self.assertRaises(UnknownPartition):
            sched.partition("nope")


class ReadinessTests(unittest.TestCase):
    def test_sources_are_ready_immediately(self):
        sched = diamond_scheduler()
        self.assertTrue(sched.is_ready("S"))
        self.assertTrue(sched.is_ready("S2"))

    def test_downstream_waits_for_both_branches(self):
        sched = diamond_scheduler()
        self.assertFalse(sched.is_ready("M"))
        self.assertFalse(sched.is_ready("R"))

        sched.provide_source("S", "s-1", "exec-a")
        self.assertTrue(sched.is_ready("M"))
        self.assertFalse(sched.is_ready("R"))

        sched.provide_source("S2", "s2-1", "exec-b")
        # R also joins M, which has not produced yet.
        self.assertFalse(sched.is_ready("R"))

        sched.start("M", "m-1", "exec-a")
        sched.complete("m-1", "exec-a")
        self.assertTrue(sched.is_ready("R"))

    def test_launching_into_unready_partition_is_refused(self):
        sched = diamond_scheduler()
        result = sched.start("R", "r-1", "exec-a")
        self.assertFalse(result.started)
        self.assertEqual(result.status.value, "not_ready")

    def test_launchable_skips_in_flight(self):
        sched = diamond_scheduler()
        sched.provide_source("S", "s-1", "exec-a")
        sched.provide_source("S2", "s2-1", "exec-b")
        # Only M can launch: R joins M and is therefore still blocked.
        self.assertEqual(sched.launchable(), ["M"])
        sched.start("M", "m-1", "exec-a")
        self.assertEqual(sched.launchable(), [])

    def test_wide_diamond_requires_join_of_both_branches(self):
        sched = wide_diamond_scheduler()
        self.assertEqual(set(sched.ready_partitions()), {"A", "C"})
        sched.provide_source("A", "a-1", "e1")
        sched.provide_source("C", "c-1", "e2")
        self.assertEqual(set(sched.ready_partitions()), {"B", "D"})
        sched.start("B", "b-1", "e1")
        sched.complete("b-1", "e1")
        self.assertFalse(sched.is_ready("J"))
        sched.start("D", "d-1", "e2")
        sched.complete("d-1", "e2")
        self.assertTrue(sched.is_ready("J"))


if __name__ == "__main__":
    unittest.main()
