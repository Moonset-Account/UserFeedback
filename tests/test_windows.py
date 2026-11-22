"""Tests for window assignment, one-sample-per-request, and rule-at-entry."""

import unittest

from sli_adjudicator import (
    OUTCOME_PENDING,
    OUTCOME_SUCCESS,
    STATUS_SUCCESS,
    STREAM_DOWNSTREAM,
    STREAM_EDGE,
    STREAM_RECEIPT,
    Adjudicator,
    Observation,
    Rule,
)


def _rule(effective_from=0.0, allowed=500.0, receipt=True, deadline=5000.0):
    return Rule(
        effective_from=effective_from,
        allowed_latency_ms=allowed,
        requires_receipt=receipt,
        user_deadline_ms=deadline,
    )


def _ok(rid, entry, **kw):
    kw.setdefault("attempt", 0)
    kw.setdefault("version", 1)
    kw.setdefault("stream", STREAM_RECEIPT)
    kw.setdefault("status", STATUS_SUCCESS)
    kw.setdefault("latency_ms", 100.0)
    kw.setdefault("receipt_present", True)
    kw.setdefault("observed_at", entry + 0.1)
    return Observation(request_id=rid, entry_time=entry, **kw)


class WindowAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.adj = Adjudicator([_rule()], window_size_s=10.0, threshold="0.5")

    def test_window_of_floor(self):
        self.assertEqual(self.adj.window_of(0.0)[0], 0)
        self.assertEqual(self.adj.window_of(9.999)[0], 0)
        self.assertEqual(self.adj.window_of(10.0)[0], 1)
        self.assertEqual(self.adj.window_of(19.99)[0], 1)

    def test_negative_time_windows(self):
        wid, start, end = self.adj.window_of(-0.5)
        self.assertEqual(wid, -1)
        self.assertEqual(start, -10.0)
        self.assertEqual(end, 0.0)

    def test_entry_time_not_observed_time_decides_window(self):
        # Enters at 9.9 but is only observed at 30.0 -> still window 0.
        rec = Observation(
            request_id="late-obs",
            entry_time=9.9,
            attempt=0,
            version=1,
            stream=STREAM_RECEIPT,
            status=STATUS_SUCCESS,
            latency_ms=50.0,
            receipt_present=True,
            observed_at=30.0,
        )
        self.adj.ingest(rec)
        self.assertEqual(self.adj.decision(0).total, 1)
        self.assertEqual(self.adj.decision(3).total, 0)

    def test_observed_before_entry_rejected(self):
        with self.assertRaises(ValueError):
            Observation(
                request_id="bad",
                entry_time=10.0,
                attempt=0,
                version=1,
                stream=STREAM_EDGE,
                status=STATUS_SUCCESS,
                observed_at=9.0,
            )


class OneSamplePerRequestTests(unittest.TestCase):
    def setUp(self):
        self.adj = Adjudicator([_rule()], window_size_s=10.0, threshold="0.5")

    def test_repeated_sightings_collapse_to_one_sample(self):
        self.adj.ingest(_ok("r", 1.0, stream=STREAM_EDGE, latency_ms=90.0))
        self.adj.ingest(_ok("r", 1.0, stream=STREAM_DOWNSTREAM, latency_ms=95.0))
        self.adj.ingest(_ok("r", 1.0, stream=STREAM_RECEIPT, latency_ms=100.0))
        dec = self.adj.decision(0)
        self.assertEqual(dec.total, 1)
        self.assertEqual(dec.success, 1)

    def test_distinct_requests_same_window(self):
        for i in range(5):
            self.adj.ingest(_ok(f"r{i}", 1.0 + i))
        self.assertEqual(self.adj.decision(0).total, 5)

    def test_mutable_entry_time_rejected(self):
        self.adj.ingest(_ok("r", 1.0))
        with self.assertRaises(ValueError):
            self.adj.ingest(_ok("r", 2.0))

    def test_final_receipt_beats_partial_success_same_version(self):
        # partial success (no receipt, downstream) first -> pending
        self.adj.ingest(
            Observation(
                request_id="r",
                entry_time=1.0,
                attempt=0,
                version=1,
                stream=STREAM_DOWNSTREAM,
                status=STATUS_SUCCESS,
                latency_ms=100.0,
                receipt_present=False,
                observed_at=1.1,
            )
        )
        self.assertEqual(self.adj.request_outcome("r"), OUTCOME_PENDING)
        # final receipt, same version -> success
        self.adj.ingest(
            Observation(
                request_id="r",
                entry_time=1.0,
                attempt=0,
                version=1,
                stream=STREAM_RECEIPT,
                status=STATUS_SUCCESS,
                latency_ms=100.0,
                receipt_present=True,
                observed_at=1.2,
            )
        )
        self.assertEqual(self.adj.request_outcome("r"), OUTCOME_SUCCESS)
        self.assertEqual(self.adj.decision(0).success, 1)


class RuleAtEntryTests(unittest.TestCase):
    def test_rule_chosen_by_entry_not_observation_time(self):
        # Old rule (before 100) needs no receipt; new rule (from 100) does.
        # A generous deadline keeps the deadline gate out of the picture so the
        # test isolates which *rule* is applied.
        old = _rule(effective_from=0.0, allowed=500.0, receipt=False, deadline=10_000_000.0)
        new = _rule(effective_from=100.0, allowed=500.0, receipt=True, deadline=10_000_000.0)
        adj = Adjudicator([new, old], window_size_s=100.0, threshold="0.5")

        # Enters at 50 (old rule, no receipt needed) but observed at 150.
        adj.ingest(
            Observation(
                request_id="pre-change",
                entry_time=50.0,
                attempt=0,
                version=1,
                stream=STREAM_DOWNSTREAM,
                status=STATUS_SUCCESS,
                latency_ms=100.0,
                receipt_present=False,
                observed_at=150.0,
            )
        )
        # Enters at 150 (new rule, receipt required) -> pending.
        adj.ingest(
            Observation(
                request_id="post-change",
                entry_time=150.0,
                attempt=0,
                version=1,
                stream=STREAM_DOWNSTREAM,
                status=STATUS_SUCCESS,
                latency_ms=100.0,
                receipt_present=False,
                observed_at=150.1,
            )
        )
        self.assertEqual(adj.request_outcome("pre-change"), OUTCOME_SUCCESS)
        self.assertEqual(adj.request_outcome("post-change"), OUTCOME_PENDING)

    def test_no_rule_before_earliest_raises(self):
        adj = Adjudicator([_rule(effective_from=10.0)], window_size_s=10.0, threshold="0.5")
        with self.assertRaises(ValueError):
            adj.ingest(_ok("early", 5.0))
        # sanity: rule_at boundary is inclusive
        self.assertEqual(adj.rule_at(10.0).effective_from, 10.0)

    def test_rule_at_picks_latest_effective(self):
        r0 = _rule(0.0)
        r5 = _rule(5.0)
        adj = Adjudicator([r0, r5], window_size_s=10.0, threshold="0.5")
        self.assertIs(adj.rule_at(4.999), r0)
        self.assertIs(adj.rule_at(5.0), r5)
        self.assertIs(adj.rule_at(1000.0), r5)


if __name__ == "__main__":
    unittest.main()
