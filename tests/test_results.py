"""Acceptance tests: result registration and first-success-wins semantics."""

from __future__ import annotations

import unittest

from shufflesched import BlockId, CompletionStatus, ShuffleScheduler, StartStatus

from .helpers import diamond_scheduler, wide_diamond_scheduler


class FirstSuccessWinsTests(unittest.TestCase):
    def test_first_of_two_speculative_attempts_provides_the_block(self):
        sched = diamond_scheduler()
        sched.provide_source("S", "s-1", "exec-a")
        sched.provide_source("S2", "s2-1", "exec-b")

        first = sched.start("M", "m-1", "exec-a")
        second = sched.start("M", "m-2", "exec-b")
        self.assertTrue(first.started)
        self.assertTrue(second.started)
        self.assertEqual(sorted(sched.active_attempts("M")), ["m-1", "m-2"])

        winner = sched.complete("m-1", "exec-a")
        self.assertTrue(winner.accepted)
        self.assertEqual(winner.block.id, BlockId("M", 0, "m-1"))
        self.assertEqual(sched.winning_id("M"), BlockId("M", 0, "m-1"))

        loser = sched.complete("m-2", "exec-b")
        self.assertFalse(loser.accepted)
        self.assertIs(loser.status, CompletionStatus.REJECTED_ALREADY_WON)
        # The winner is untouched by the losing speculative attempt.
        self.assertEqual(sched.winning_id("M"), BlockId("M", 0, "m-1"))

    def test_second_attempt_can_win_when_it_finishes_first(self):
        sched = diamond_scheduler()
        sched.provide_source("S", "s-1", "exec-a")
        sched.provide_source("S2", "s2-1", "exec-b")
        sched.start("M", "m-1", "exec-a")
        sched.start("M", "m-2", "exec-b")

        winner = sched.complete("m-2", "exec-b")
        self.assertTrue(winner.accepted)
        self.assertEqual(sched.winning_id("M"), BlockId("M", 0, "m-2"))

        loser = sched.complete("m-1", "exec-a")
        self.assertIs(loser.status, CompletionStatus.REJECTED_ALREADY_WON)

    def test_losing_attempt_that_reports_twice_is_rejected_as_duplicate(self):
        sched = diamond_scheduler()
        sched.provide_source("S", "s-1", "exec-a")
        sched.provide_source("S2", "s2-1", "exec-b")
        sched.start("M", "m-1", "exec-a")
        sched.start("M", "m-2", "exec-b")
        sched.complete("m-1", "exec-a")
        sched.complete("m-2", "exec-b")

        repeated = sched.complete("m-2", "exec-b")
        self.assertIs(repeated.status, CompletionStatus.REJECTED_DUPLICATE)
        self.assertEqual(sched.winning_id("M"), BlockId("M", 0, "m-1"))

    def test_unknown_attempt_callback_is_rejected(self):
        sched = diamond_scheduler()
        result = sched.complete("ghost", None)
        self.assertIs(result.status, CompletionStatus.REJECTED_UNKNOWN_ATTEMPT)

    def test_callback_from_wrong_executor_is_rejected(self):
        sched = diamond_scheduler()
        sched.provide_source("S", "s-1", "exec-a")
        sched.provide_source("S2", "s2-1", "exec-b")
        sched.start("M", "m-1", "exec-a")
        result = sched.complete("m-1", "exec-impostor")
        self.assertIs(result.status, CompletionStatus.REJECTED_UNKNOWN_ATTEMPT)
        self.assertIsNone(sched.winning_id("M"))

    def test_duplicate_attempt_request_is_idempotent_via_start(self):
        sched = diamond_scheduler()
        sched.provide_source("S", "s-1", "exec-a")
        sched.provide_source("S2", "s2-1", "exec-b")
        sched.start("M", "m-1", "exec-a")
        again = sched.start("M", "m-1", "exec-a")
        self.assertFalse(again.started)
        self.assertIs(again.status, StartStatus.DUPLICATE_ATTEMPT)
        # Strict registration path refuses a reused id outright.
        from shufflesched import DuplicateAttempt

        with self.assertRaises(DuplicateAttempt):
            sched.request_attempt(partition="M", attempt_id="m-1", executor="exec-a")


class ResultReadSetTests(unittest.TestCase):
    def test_result_records_the_upstream_blocks_it_actually_read(self):
        sched = wide_diamond_scheduler()
        sched.provide_source("A", "a-1", "e1")
        sched.provide_source("C", "c-1", "e2")

        sched.start("B", "b-1", "e1")
        b = sched.complete("b-1", "e1")
        self.assertTrue(b.accepted)
        self.assertEqual(sched.consumed("B"), {"A": BlockId("A", 0, "a-1")})

        sched.start("D", "d-1", "e2")
        sched.complete("d-1", "e2")
        sched.start("J", "j-1", "e3")
        j = sched.complete("j-1", "e3")
        self.assertTrue(j.accepted)
        self.assertEqual(
            sched.consumed("J"),
            {"B": BlockId("B", 0, "b-1"), "D": BlockId("D", 0, "d-1")},
        )
    def test_narrow_child_can_report_a_narrow_read_set(self):
        sched = diamond_scheduler()
        sched.provide_source("S", "s-1", "exec-a")
        sched.start("M", "m-1", "exec-a", )
        # Explicit read set naming the winning upstream block.
        result = sched.complete("m-1", "exec-a", reads={"S": "s-1"})
        self.assertTrue(result.accepted)
        self.assertEqual(sched.consumed("M"), {"S": BlockId("S", 0, "s-1")})

    def test_reading_a_non_declared_partition_is_rejected(self):
        sched = diamond_scheduler()
        sched.provide_source("S", "s-1", "exec-a")
        sched.provide_source("S2", "s2-1", "exec-b")
        sched.start("M", "m-1", "exec-a")
        # M declares only S as input; naming S2 is illegal.
        result = sched.complete("m-1", "exec-a", reads={"S2": "s2-1"})
        self.assertIs(result.status, CompletionStatus.REJECTED_INVALID_READ)
        self.assertIsNone(sched.winning_id("M"))

    def test_reading_a_non_winning_block_is_rejected_as_stale(self):
        sched = wide_diamond_scheduler()
        sched.provide_source("A", "a-1", "e1")
        sched.start("B", "b-1", "e1")
        sched.start("B", "b-2", "e2")

        # b-2 claims it read a block id that never existed for A.
        forged = BlockId("A", 0, "a-999")
        loser = sched.complete("b-2", "e2", reads={"A": forged})
        self.assertIs(loser.status, CompletionStatus.REJECTED_STALE_INPUTS)
        self.assertIsNone(sched.winning_id("B"))

        # The honest attempt still installs normally afterwards.
        winner = sched.complete("b-1", "e1")
        self.assertTrue(winner.accepted)
        self.assertEqual(sched.winning_id("B"), BlockId("B", 0, "b-1"))

    def test_source_cannot_claim_an_upstream_read(self):
        sched = diamond_scheduler()
        sched.provide_source("S", "s-1", "exec-a")
        sched.start("M", "m-1", "exec-a")
        # A source attempt with reads is malformed; S is already done, so use a
        # fresh speculative source attempt that is still in flight.
        sched2 = ShuffleScheduler()
        sched2.add_partition("S")
        sched2.add_partition("S3")
        sched2.seal()
        sched2.provide_source("S3", "s3-1", "exec-b")
        sched2.start("S", "s-2", "exec-a")
        result = sched2.complete("s-2", "exec-a", reads={"S3": "s3-1"})
        self.assertIs(result.status, CompletionStatus.REJECTED_INVALID_READ)
        self.assertIsNone(sched2.winning_id("S"))


if __name__ == "__main__":
    unittest.main()
