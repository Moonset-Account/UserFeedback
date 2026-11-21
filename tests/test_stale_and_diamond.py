"""Acceptance tests for stale-round handling and a full diamond failure replay.

These cover the three interlocking requirements:

* a success callback from an older round can never replace a newer block;
* a duplicate loss notification never re-invalidates a fresh result;
* in a diamond, the join recomputes exactly once after a failure, and a late
  old attempt can neither install a block nor make the join ready early.
"""

from __future__ import annotations

import unittest

from shufflesched import BlockId, CompletionStatus, LossStatus, ShuffleScheduler


class StaleSuccessCallbackTests(unittest.TestCase):
    def test_old_round_callback_cannot_replace_the_new_block(self):
        sched = ShuffleScheduler()
        sched.add_partition("S")
        sched.add_partition("M", ["S"], kind="narrow")
        sched.seal()

        sched.provide_source("S", "s-1", "e-dead")
        sched.start("M", "m-1", "e-dead")          # epoch 0 of M
        self.assertTrue(sched.complete("m-1", "e-dead").accepted)

        # The winning block is lost; M is reopened and its epoch bumped.
        sched.report_executor_lost("loss-1", "e-dead")
        self.assertEqual(sched.partition("M").epoch, 1)
        self.assertFalse(sched.committed("M"))

        # The recomputation wins on a new attempt.
        sched.provide_source("S", "s-2", "e-live")
        sched.start("M", "m-2", "e-live")
        self.assertTrue(sched.complete("m-2", "e-live").accepted)
        self.assertEqual(sched.winning_id("M"), BlockId("M", 1, "m-2"))

        # Now a very late callback for the *old* attempt arrives.
        late = sched.complete("m-1", "e-dead")
        self.assertFalse(late.accepted)
        self.assertIs(late.status, CompletionStatus.REJECTED_DUPLICATE)

        # Even with a brand-new callback object for the same stale attempt the
        # winner cannot be displaced.
        self.assertEqual(sched.winning_id("M"), BlockId("M", 1, "m-2"))

    def test_stale_attempt_that_never_reported_is_rejected_on_late_success(self):
        sched = ShuffleScheduler()
        sched.add_partition("S")
        sched.add_partition("M", ["S"], kind="narrow")
        sched.seal()

        sched.provide_source("S", "s-1", "e-dead")
        sched.start("M", "m-old", "e-dead")        # epoch 0, will be left in flight
        sched.report_executor_lost("loss-1", "e-dead")

        # Recompute and win at epoch 1.
        sched.provide_source("S", "s-2", "e-live")
        sched.start("M", "m-new", "e-live")
        self.assertTrue(sched.complete("m-new", "e-live").accepted)

        # The old attempt reports success *after* losing its executor.
        late = sched.complete("m-old", "e-dead")
        self.assertIs(late.status, CompletionStatus.REJECTED_STALE_EPOCH)
        # M never committed a round-0 block, so its epoch did not need to bump;
        # the orphaned-attempt guard is what blocks the stale callback.
        self.assertEqual(sched.partition("M").epoch, 0)
        self.assertEqual(sched.winning_id("M"), BlockId("M", 0, "m-new"))

    def test_losing_attempt_from_the_same_round_is_marked_duplicate_not_installed(self):
        sched = ShuffleScheduler()
        sched.add_partition("S")
        sched.add_partition("M", ["S"], kind="narrow")
        sched.seal()
        sched.provide_source("S", "s-1", "e1")
        sched.start("M", "m-1", "e1")
        sched.start("M", "m-2", "e2")
        sched.complete("m-1", "e1")

        late = sched.complete("m-2", "e2")
        self.assertIs(late.status, CompletionStatus.REJECTED_ALREADY_WON)
        self.assertEqual(sched.winning_id("M"), BlockId("M", 0, "m-1"))


class DiamondReplayTests(unittest.TestCase):
    """A diamond whose left branch executor dies after the join completes."""

    def _build_and_run(self):
        sched = ShuffleScheduler()
        sched.add_partition("A")
        sched.add_partition("C")
        sched.add_partition("B", ["A"], kind="narrow")
        sched.add_partition("D", ["C"], kind="narrow")
        sched.add_partition("J", ["B", "D"], kind="shuffle")
        sched.seal()

        sched.provide_source("A", "a-1", "e-left")
        sched.provide_source("C", "c-1", "e-right")
        sched.start("B", "b-1", "e-left")
        self.assertTrue(sched.complete("b-1", "e-left").accepted)
        sched.start("D", "d-1", "e-right")
        self.assertTrue(sched.complete("d-1", "e-right").accepted)
        sched.start("J", "j-1", "e-right")
        self.assertTrue(sched.complete("j-1", "e-right").accepted)
        return sched

    def test_join_recomputes_exactly_once_after_left_executor_loss(self):
        sched = self._build_and_run()
        self.assertEqual(sched.recompute_count("J"), 0)

        report = sched.report_executor_lost("loss-1", "e-left")
        self.assertEqual(report.invalidated, ["A", "B", "J"])
        self.assertEqual(report.preserved, ["C", "D"])

        # Recompute the affected branch and the join.
        sched.provide_source("A", "a-2", "e-new")
        sched.start("B", "b-2", "e-new")
        self.assertTrue(sched.complete("b-2", "e-new").accepted)
        sched.start("J", "j-2", "e-right")
        self.assertTrue(sched.complete("j-2", "e-right").accepted)

        # The join sees exactly one extra computation, not two.
        self.assertEqual(sched.recompute_count("J"), 1)
        self.assertEqual(sched.partition("J").result_count, 2)
        self.assertEqual(sched.winning_id("J"), BlockId("J", 1, "j-2"))

    def test_join_is_not_ready_before_recomputed_parent(self):
        sched = self._build_and_run()
        sched.report_executor_lost("loss-1", "e-left")

        # A is gone, so B cannot be ready and neither can J.
        self.assertFalse(sched.is_ready("B"))
        self.assertFalse(sched.is_ready("J"))
        # D survived, so only the left branch blocks the join.
        self.assertTrue(sched.committed("D"))

        sched.provide_source("A", "a-2", "e-new")
        self.assertTrue(sched.is_ready("B"))
        self.assertFalse(sched.is_ready("J"))

        sched.start("B", "b-2", "e-new")
        sched.complete("b-2", "e-new")
        self.assertTrue(sched.is_ready("J"))

    def test_late_old_join_attempt_cannot_make_join_ready_or_win_early(self):
        sched = self._build_and_run()
        sched.report_executor_lost("loss-1", "e-left")

        # Before the join recomputes, a stale old join callback arrives.
        stale_join = sched.complete("j-1", "e-right")
        self.assertIs(stale_join.status, CompletionStatus.REJECTED_DUPLICATE)
        self.assertFalse(sched.committed("J"))
        # The stale callback did not resurrect the lost block either.
        self.assertFalse(sched.committed("B"))

        # Recompute properly; only now does the join become ready and win once.
        sched.provide_source("A", "a-2", "e-new")
        sched.start("B", "b-2", "e-new")
        sched.complete("b-2", "e-new")
        self.assertTrue(sched.is_ready("J"))
        sched.start("J", "j-2", "e-right")
        self.assertTrue(sched.complete("j-2", "e-right").accepted)
        self.assertEqual(sched.recompute_count("J"), 1)

    def test_duplicate_loss_notification_does_not_reopen_the_join_again(self):
        sched = self._build_and_run()
        sched.report_executor_lost("loss-1", "e-left")

        # Recompute everything on healthy executors.
        sched.provide_source("A", "a-2", "e-new")
        sched.start("B", "b-2", "e-new")
        sched.complete("b-2", "e-new")
        sched.start("J", "j-2", "e-right")
        sched.complete("j-2", "e-right")
        self.assertTrue(sched.committed("J"))

        duplicate = sched.report_executor_lost("loss-1", "e-left")
        self.assertIs(duplicate.status, LossStatus.DUPLICATE_EVENT)
        self.assertTrue(sched.committed("J"))
        self.assertEqual(sched.recompute_count("J"), 1)

    def test_right_branch_can_keep_serving_the_join_while_left_recomputes(self):
        # D and J's read of D remain valid; only B's block is gone.
        sched = self._build_and_run()
        consumed_d = sched.consumed("D")
        sched.report_executor_lost("loss-1", "e-left")
        self.assertEqual(sched.consumed("D"), consumed_d)
        self.assertTrue(sched.committed("D"))


if __name__ == "__main__":
    unittest.main()
