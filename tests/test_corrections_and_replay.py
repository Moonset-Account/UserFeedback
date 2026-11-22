"""Tests for sealing, late corrections, the incremental log, and the replay."""

import unittest

from sli_adjudicator import (
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    STATUS_FAILURE,
    STATUS_SUCCESS,
    STREAM_EDGE,
    STREAM_RECEIPT,
    VERDICT_ALLOW,
    VERDICT_REJECT,
    VERDICT_WAIT,
    Adjudicator,
    Correction,
    Observation,
    Rule,
)
from sli_adjudicator.scenarios import replay_failure_retry_correction


def _rule(**kw):
    kw.setdefault("effective_from", 0.0)
    kw.setdefault("allowed_latency_ms", 500.0)
    kw.setdefault("requires_receipt", True)
    kw.setdefault("user_deadline_ms", 5000.0)
    return Rule(**kw)


def _ok(rid, entry, observed=None, latency=100.0):
    return Observation(
        request_id=rid,
        entry_time=entry,
        attempt=0,
        version=1,
        stream=STREAM_RECEIPT,
        status=STATUS_SUCCESS,
        latency_ms=latency,
        receipt_present=True,
        observed_at=(entry + 0.1) if observed is None else observed,
    )


class SealTests(unittest.TestCase):
    def test_window_seals_after_lateness(self):
        adj = Adjudicator([_rule()], window_size_s=10.0, threshold="0.5",
                          allowed_lateness_s=2.0)
        adj.ingest(_ok("a", 1.0))
        self.assertFalse(adj.decision(0).sealed)
        # watermark 12.0 == end(10) + lateness(2) -> seals
        adj.advance_watermark(12.0)
        self.assertTrue(adj.decision(0).sealed)

    def test_window_not_sealed_before_lateness(self):
        adj = Adjudicator([_rule()], window_size_s=10.0, threshold="0.5",
                          allowed_lateness_s=2.0)
        adj.ingest(_ok("a", 1.0))
        adj.advance_watermark(11.999)
        self.assertFalse(adj.decision(0).sealed)

    def test_auto_seal_disabled(self):
        adj = Adjudicator([_rule()], window_size_s=10.0, threshold="0.5",
                          allowed_lateness_s=0.0, auto_seal=False)
        adj.ingest(_ok("a", 1.0))
        adj.ingest(_ok("b", 100.0))
        self.assertFalse(adj.decision(0).sealed)
        # explicit seal still works
        adj.seal_window(0)
        self.assertTrue(adj.decision(0).sealed)

    def test_explicit_advance_watermark_seals_even_with_auto_seal_off(self):
        adj = Adjudicator([_rule()], window_size_s=10.0, threshold="0.5",
                          allowed_lateness_s=0.0, auto_seal=False)
        adj.ingest(_ok("a", 1.0))
        adj.ingest(_ok("b", 100.0))  # ingest-driven advance is suppressed
        self.assertFalse(adj.decision(0).sealed)
        # An explicit watermark advance is honoured regardless of auto_seal.
        adj.advance_watermark(10.0)
        self.assertTrue(adj.decision(0).sealed)

    def test_seal_entry_in_log(self):
        adj = Adjudicator([_rule()], window_size_s=10.0, threshold="0.5",
                          allowed_lateness_s=0.0)
        adj.ingest(_ok("a", 1.0))
        adj.ingest(_ok("b", 25.0))  # watermark 25.1 seals window 0 (end 10)
        seals = adj.seal_entries(0)
        self.assertEqual(len(seals), 1)
        self.assertEqual(seals[0].cause, "seal")
        self.assertTrue(seals[0].applied)


class LateCorrectionTests(unittest.TestCase):
    def _setup(self):
        # threshold 0.5; window open; nothing sealed yet (no watermark early).
        return Adjudicator([_rule(user_deadline_ms=100000.0)], window_size_s=10.0,
                           threshold="0.5", allowed_lateness_s=1000.0)

    def test_correction_changes_counts_before_seal(self):
        adj = self._setup()
        adj.ingest(
            Observation("r", 1.0, 0, 1, STREAM_EDGE, STATUS_FAILURE, observed_at=1.1)
        )
        self.assertEqual(adj.decision(0).failure, 1)
        adj.ingest(
            Correction("r", 1.0, 1, 2, STATUS_SUCCESS, 90.0, True, observed_at=1.5)
        )
        dec = adj.decision(0)
        self.assertEqual(dec.failure, 0)
        self.assertEqual(dec.success, 1)
        self.assertFalse(dec.sealed)

    def test_correction_after_seal_not_applied(self):
        adj = Adjudicator([_rule(user_deadline_ms=100000.0)], window_size_s=10.0,
                          threshold="0.5", allowed_lateness_s=0.0)
        adj.ingest(
            Observation("r", 1.0, 0, 1, STREAM_EDGE, STATUS_FAILURE, observed_at=1.1)
        )
        adj.advance_watermark(10.0)  # seal window 0 at end+lateness
        self.assertTrue(adj.decision(0).sealed)
        before = adj.decision(0)
        entries = adj.ingest(
            Correction("r", 1.0, 1, 2, STATUS_SUCCESS, 90.0, True,
                       observed_at=11.0)
        )
        applied = [e for e in entries if e.request_id == "r"]
        self.assertTrue(applied)
        self.assertFalse(applied[0].applied)
        self.assertEqual(applied[0].reason, "window sealed")
        after = adj.decision(0)
        self.assertEqual(after.failure, before.failure)
        self.assertEqual(after.success, before.success)
        self.assertEqual(after.verdict, before.verdict)
        self.assertTrue(after.sealed)

    def test_sealed_verdict_retained_even_if_would_flip(self):
        # Two failures -> reject; late correction would make it allow, but the
        # window is sealed, so the reject stands.
        adj = Adjudicator([_rule(user_deadline_ms=100000.0)], window_size_s=10.0,
                          threshold="0.4", allowed_lateness_s=0.0)
        adj.ingest(Observation("a", 1.0, 0, 1, STREAM_EDGE, STATUS_FAILURE, observed_at=1.1))
        adj.ingest(Observation("b", 2.0, 0, 1, STREAM_EDGE, STATUS_FAILURE, observed_at=2.1))
        self.assertEqual(adj.decision(0).verdict, VERDICT_REJECT)
        adj.advance_watermark(10.0)
        adj.ingest(Correction("a", 1.0, 1, 2, STATUS_SUCCESS, 50.0, True, observed_at=11.0))
        self.assertEqual(adj.decision(0).verdict, VERDICT_REJECT)
        self.assertEqual(adj.decision(0).failure, 2)


class IncrementalLogTests(unittest.TestCase):
    def test_log_records_deltas(self):
        adj = Adjudicator([_rule(user_deadline_ms=100000.0)], window_size_s=10.0,
                          threshold="0.5", allowed_lateness_s=1000.0)
        out1 = adj.ingest(_ok("r", 1.0))
        self.assertEqual(out1[0].delta_success, 1)
        self.assertEqual(out1[0].delta_failure, 0)
        out2 = adj.ingest(
            Correction("r", 1.0, 0, 2, STATUS_FAILURE, 100.0, True, observed_at=2.0)
        )
        e = out2[0]
        self.assertEqual(e.delta_success, -1)
        self.assertEqual(e.delta_failure, 1)
        self.assertEqual(e.prev_outcome, OUTCOME_SUCCESS)
        self.assertEqual(e.new_outcome, OUTCOME_FAILURE)
        self.assertEqual(e.counts.failure, 1)
        self.assertEqual(e.counts.success, 0)

    def test_log_seq_monotonic(self):
        adj = Adjudicator([_rule()], window_size_s=10.0, threshold="0.5")
        adj.ingest(_ok("a", 1.0))
        adj.ingest(_ok("b", 2.0))
        adj.ingest(_ok("c", 3.0))
        seqs = [e.seq for e in adj.log()]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))

    def test_timeline_filters_by_request(self):
        adj = Adjudicator([_rule(user_deadline_ms=100000.0)], window_size_s=10.0,
                          threshold="0.3", allowed_lateness_s=1000.0)
        adj.ingest(_ok("a", 1.0))
        adj.ingest(_ok("b", 2.0))
        adj.ingest(Correction("a", 1.0, 0, 2, STATUS_FAILURE, 100.0, True, observed_at=3.0))
        tl = adj.timeline("a")
        self.assertEqual(len(tl), 2)
        self.assertTrue(all(e.request_id == "a" for e in tl))
        # First sample of an empty window: before the request there are no
        # samples, so the verdict *before* is wait; one clean sample -> allow.
        self.assertEqual(tl[0].verdict_before, VERDICT_WAIT)
        self.assertEqual(tl[0].verdict_after, VERDICT_ALLOW)
        # The correction flips 1 success -> 1 failure out of 2: 1/2 > 0.3 -> reject.
        self.assertEqual(tl[1].verdict_before, VERDICT_ALLOW)
        self.assertEqual(tl[1].verdict_after, VERDICT_REJECT)

    def test_verdict_transition_captured(self):
        adj = Adjudicator([_rule(user_deadline_ms=100000.0)], window_size_s=10.0,
                          threshold="0.3", allowed_lateness_s=1000.0)
        adj.ingest(_ok("a", 1.0))
        adj.ingest(_ok("b", 2.0))
        entry = adj.ingest(
            Observation("c", 3.0, 0, 1, STREAM_EDGE, STATUS_FAILURE, observed_at=3.1)
        )[0]
        # 1/3 failure > 0.3 threshold -> reject, started allow.
        self.assertEqual(entry.verdict_before, VERDICT_ALLOW)
        self.assertEqual(entry.verdict_after, VERDICT_REJECT)


class ReplayScenarioTests(unittest.TestCase):
    def setUp(self):
        self.result = replay_failure_retry_correction()
        self.adj = self.result.adjudicator

    def test_request_lands_in_window_one(self):
        self.assertEqual(self.result.target_window, 1)
        self.assertEqual(self.adj.request_outcome("req-42"), OUTCOME_SUCCESS)

    def test_story_timeline(self):
        timeline = self.result.verdict_timeline()
        self.assertEqual(
            timeline,
            [
                (11.1, VERDICT_ALLOW),   # fillers keep it clean
                (12.2, VERDICT_REJECT),  # initial failure reported
                (13.0, VERDICT_WAIT),    # partial retry success -> pending
                (20.5, VERDICT_ALLOW),   # late correction confirms success
            ],
        )

    def test_final_decision_is_allow_and_sealed(self):
        dec = self.result.final_decision()
        self.assertEqual(dec.verdict, VERDICT_ALLOW)
        self.assertTrue(dec.sealed)
        self.assertEqual(dec.total, 10)
        self.assertEqual(dec.failure, 0)

    def test_post_seal_correction_rejected(self):
        post = next(s for s in self.result.steps if s.label == "post-seal-correction")
        self.assertEqual(post.verdict, VERDICT_ALLOW)
        self.assertEqual(post.failure, 0)

    def test_only_one_sample_for_target(self):
        # 9 fillers + req-42 == 10 total, despite 4 sightings of req-42.
        self.assertEqual(self.result.final_decision().total, 10)

    def test_bound_before_and_after_pending(self):
        reject_step = next(s for s in self.result.steps if s.label == "retry-failure")
        self.assertAlmostEqual(reject_step.lower_bound, 0.1)
        self.assertAlmostEqual(reject_step.upper_bound, 0.1)
        wait_step = next(s for s in self.result.steps if s.label == "retry-partial-success")
        self.assertAlmostEqual(wait_step.lower_bound, 0.0)
        self.assertAlmostEqual(wait_step.upper_bound, 0.1)


if __name__ == "__main__":
    unittest.main()
