"""Acceptance tests: executor loss, selective invalidation and recomputation."""

from __future__ import annotations

import unittest

from shufflesched import BlockId, CompletionStatus, LossStatus, ShuffleScheduler

from .helpers import diamond_scheduler, wide_diamond_scheduler


class SelectiveInvalidationTests(unittest.TestCase):
    def test_losing_a_winning_block_invalidates_only_downstream_results(self):
        sched = wide_diamond_scheduler()
        sched.provide_source("A", "a-1", "e-dead")
        sched.provide_source("C", "c-1", "e-live")
        sched.start("B", "b-1", "e-dead")
        sched.complete("b-1", "e-dead")
        sched.start("D", "d-1", "e-live")
        sched.complete("d-1", "e-live")
        sched.start("J", "j-1", "e-live")
        sched.complete("j-1", "e-live")

        report = sched.report_executor_lost("loss-1", "e-dead")

        self.assertIs(report.status, LossStatus.APPLIED)
        self.assertEqual(report.lost_blocks, [BlockId("A", 0, "a-1"),
                                              BlockId("B", 0, "b-1")])
        # A and B owned lost blocks, J read B.
        self.assertEqual(report.reopened, ["A", "B"])
        self.assertEqual(report.invalidated, ["A", "B", "J"])
        # C and D never read anything from the dead executor.
        self.assertEqual(report.preserved, ["C", "D"])

        self.assertFalse(sched.committed("A"))
        self.assertFalse(sched.committed("B"))
        self.assertFalse(sched.committed("J"))
        self.assertTrue(sched.committed("C"))
        self.assertTrue(sched.committed("D"))
        self.assertEqual(sched.winning_id("D"), BlockId("D", 0, "d-1"))

    def test_reopened_partition_epoch_is_bumped(self):
        sched = wide_diamond_scheduler()
        sched.provide_source("A", "a-1", "e-dead")
        sched.start("B", "b-1", "e-dead")
        sched.complete("b-1", "e-dead")
        before = sched.partition("B").epoch

        sched.report_executor_lost("loss-1", "e-dead")
        self.assertEqual(sched.partition("B").epoch, before + 1)
        self.assertEqual(sched.partition("C").epoch, 0)  # untouched

    def test_preserved_branch_keeps_its_read_set(self):
        sched = wide_diamond_scheduler()
        sched.provide_source("A", "a-1", "e-dead")
        sched.provide_source("C", "c-1", "e-live")
        sched.start("D", "d-1", "e-live")
        sched.complete("d-1", "e-live")
        consumed_before = sched.consumed("D")

        sched.report_executor_lost("loss-1", "e-dead")
        self.assertTrue(sched.committed("D"))
        self.assertEqual(sched.consumed("D"), consumed_before)

    def test_loss_with_no_blocks_from_executor_is_a_noop(self):
        sched = wide_diamond_scheduler()
        sched.provide_source("A", "a-1", "e1")
        report = sched.report_executor_lost("loss-1", "e-unknown")
        self.assertIs(report.status, LossStatus.NO_LOSS)
        self.assertTrue(sched.committed("A"))


class NarrowDependencyTests(unittest.TestCase):
    def test_narrow_parent_loss_recomputes_child_and_join(self):
        sched = diamond_scheduler()
        sched.provide_source("S", "s-1", "e-dead")
        sched.provide_source("S2", "s2-1", "e-live")
        sched.start("M", "m-1", "e-dead")
        sched.complete("m-1", "e-dead")
        sched.start("R", "r-1", "e-live")
        sched.complete("r-1", "e-live")

        report = sched.report_executor_lost("loss-1", "e-dead")
        self.assertEqual(report.invalidated, ["M", "R", "S"])
        self.assertEqual(report.preserved, ["S2"])

    def test_multi_level_dependency_chain(self):
        # S -> M (narrow) -> P (shuffle) -> Q (shuffle): a lost S block must
        # propagate through every level, and nothing else.
        sched = ShuffleScheduler()
        sched.add_partition("S")
        sched.add_partition("OTHER")
        sched.add_partition("M", ["S"], kind="narrow")
        sched.add_partition("P", ["M"], kind="shuffle")
        sched.add_partition("Q", ["P", "OTHER"], kind="shuffle")
        sched.seal()

        sched.provide_source("S", "s-1", "e-dead")
        sched.provide_source("OTHER", "o-1", "e-live")
        for pid, aid, ex in [("M", "m-1", "e-dead"), ("P", "p-1", "e-live"),
                             ("Q", "q-1", "e-live")]:
            sched.start(pid, aid, ex)
            self.assertTrue(sched.complete(aid, ex).accepted, pid)

        report = sched.report_executor_lost("loss-1", "e-dead")
        self.assertEqual(report.invalidated, ["M", "P", "Q", "S"])
        self.assertEqual(report.preserved, ["OTHER"])


class DuplicateLossNotificationTests(unittest.TestCase):
    def test_repeated_loss_event_is_ignored_after_the_first(self):
        sched = wide_diamond_scheduler()
        sched.provide_source("A", "a-1", "e-dead")
        sched.start("B", "b-1", "e-dead")
        sched.complete("b-1", "e-dead")

        first = sched.report_executor_lost("loss-1", "e-dead")
        self.assertIs(first.status, LossStatus.APPLIED)

        # Recompute succeeds on a healthy executor.
        sched.provide_source("A", "a-2", "e-live")
        sched.start("B", "b-2", "e-live")
        self.assertTrue(sched.complete("b-2", "e-live").accepted)

        second = sched.report_executor_lost("loss-1", "e-dead")
        self.assertIs(second.status, LossStatus.DUPLICATE_EVENT)
        # The duplicate must not undo the fresh result.
        self.assertTrue(sched.committed("B"))
        self.assertEqual(sched.winning_id("B"), BlockId("B", 1, "b-2"))
        self.assertEqual(sched.recompute_count("B"), 1)

    def test_distinct_events_for_same_executor_are_both_applied(self):
        sched = wide_diamond_scheduler()
        sched.provide_source("A", "a-1", "e-dead")
        first = sched.report_executor_lost("loss-1", "e-dead")
        self.assertIs(first.status, LossStatus.APPLIED)
        # Nothing left from that executor: the second event is consistent but
        # reports no loss.
        second = sched.report_executor_lost("loss-2", "e-dead")
        self.assertIs(second.status, LossStatus.NO_LOSS)


class ActualReadSetTests(unittest.TestCase):
    """Invalidation follows what a result *actually* read, not the full DAG."""

    def _partial_join(self):
        sched = ShuffleScheduler()
        sched.add_partition("A")
        sched.add_partition("C")
        sched.add_partition("J", ["A", "C"], kind="shuffle")
        sched.seal()
        sched.provide_source("A", "a-1", "e-a")
        sched.provide_source("C", "c-1", "e-c")
        sched.start("J", "j-1", "e-j")
        # J reports it only consumed A; C is a declared but unused input.
        self.assertTrue(
            sched.complete("j-1", "e-j", reads={"A": "a-1"}).accepted
        )
        self.assertEqual(sched.consumed("J"), {"A": BlockId("A", 0, "a-1")})
        return sched

    def test_result_is_invalidated_when_a_read_block_is_lost(self):
        sched = self._partial_join()
        report = sched.report_executor_lost("loss-1", "e-a")
        self.assertIn("J", report.invalidated)
        self.assertFalse(sched.committed("J"))

    def test_result_is_preserved_when_the_lost_block_was_never_read(self):
        sched = self._partial_join()
        report = sched.report_executor_lost("loss-1", "e-c")
        self.assertEqual(report.invalidated, ["C"])
        self.assertIn("J", report.preserved)
        self.assertTrue(sched.committed("J"))
        self.assertEqual(sched.consumed("J"), {"A": BlockId("A", 0, "a-1")})

    def test_partial_join_recomputes_once_with_the_new_block(self):
        sched = self._partial_join()
        sched.report_executor_lost("loss-1", "e-a")

        sched.provide_source("A", "a-2", "e-a2")
        sched.start("J", "j-2", "e-j")
        result = sched.complete("j-2", "e-j", reads={"A": "a-2"})
        self.assertTrue(result.accepted)
        self.assertEqual(sched.recompute_count("J"), 1)
        self.assertEqual(sched.consumed("J"), {"A": BlockId("A", 1, "a-2")})


class SupersededBlockTests(unittest.TestCase):
    def test_superseded_block_is_not_reported_as_lost(self):
        sched = ShuffleScheduler()
        sched.add_partition("S")
        sched.add_partition("M", ["S"], kind="narrow")
        sched.seal()
        sched.provide_source("S", "s-1", "e-dead")
        sched.start("M", "m-1", "e-dead")
        sched.complete("m-1", "e-dead")           # block M@0/m-1 on e-dead

        # Recompute M on a healthy executor without any loss event.
        sched.start("M", "m-2", "e-live")
        self.assertFalse(sched.complete("m-2", "e-live").accepted)  # already won

        # Force a genuine supersede: lose the executor, then rebuild.
        sched.report_executor_lost("loss-1", "e-dead")
        sched.provide_source("S", "s-2", "e-live")
        sched.start("M", "m-3", "e-live")
        self.assertTrue(sched.complete("m-3", "e-live").accepted)
        self.assertEqual(sched.winning_id("M"), BlockId("M", 1, "m-3"))

        # e-live now holds S@1/s-2 and M@1/m-3; a stale report for e-dead must
        # not name the superseded M@0/m-1 block.
        report = sched.report_executor_lost("loss-2", "e-dead")
        self.assertIs(report.status, LossStatus.NO_LOSS)
        self.assertEqual(report.lost_blocks, [])
        self.assertTrue(sched.committed("M"))


class StaleInputAfterLossTests(unittest.TestCase):
    def test_in_flight_join_reporting_old_block_is_rejected_and_relaunched(self):
        sched = ShuffleScheduler()
        sched.add_partition("A")
        sched.add_partition("J", ["A"], kind="shuffle")
        sched.seal()
        sched.provide_source("A", "a-1", "e-dead")

        sched.start("J", "j-old", "e-j")  # snapshot pins A@epoch0/a-1
        sched.report_executor_lost("loss-1", "e-dead")

        # A recomputes to a fresh block before the old join reports.
        sched.provide_source("A", "a-2", "e-live")
        self.assertTrue(sched.committed("A"))

        late = sched.complete("j-old", "e-j")
        self.assertIs(late.status, CompletionStatus.REJECTED_STALE_INPUTS)
        self.assertFalse(sched.committed("J"))

        sched.start("J", "j-new", "e-j")
        self.assertTrue(sched.complete("j-new", "e-j").accepted)
        # J had no committed round before; this is its first real result.
        self.assertEqual(sched.recompute_count("J"), 0)
        self.assertEqual(sched.partition("J").result_count, 1)
        self.assertEqual(sched.consumed("J"), {"A": BlockId("A", 1, "a-2")})


if __name__ == "__main__":
    unittest.main()
