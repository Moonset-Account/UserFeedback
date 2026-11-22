"""Validation, interleaving/out-of-order reading, and concurrency-style tests.

"Concurrency" here means *interleaved event-time ingestion* (the streams arrive
out of order), which is the failure mode the module must survive without
double-counting or losing a sample.
"""

import itertools
import unittest

from sli_adjudicator import (
    OUTCOME_PENDING,
    OUTCOME_SUCCESS,
    STATUS_FAILURE,
    STATUS_SUCCESS,
    STREAM_DOWNSTREAM,
    STREAM_EDGE,
    STREAM_RECEIPT,
    Adjudicator,
    Correction,
    Observation,
    Rule,
)


def _rule(**kw):
    kw.setdefault("effective_from", 0.0)
    kw.setdefault("allowed_latency_ms", 500.0)
    kw.setdefault("requires_receipt", True)
    kw.setdefault("user_deadline_ms", 5000.0)
    return Rule(**kw)


class ValidationTests(unittest.TestCase):
    def test_bad_stream(self):
        with self.assertRaises(ValueError):
            Observation("r", 1.0, 0, 1, "carrier-pigeon", STATUS_SUCCESS)

    def test_bad_status(self):
        with self.assertRaises(ValueError):
            Observation("r", 1.0, 0, 1, STREAM_EDGE, "maybe")

    def test_empty_request_id(self):
        with self.assertRaises(ValueError):
            Observation("", 1.0, 0, 1, STREAM_EDGE, STATUS_SUCCESS)

    def test_negative_attempt(self):
        with self.assertRaises(ValueError):
            Observation("r", 1.0, -1, 1, STREAM_EDGE, STATUS_SUCCESS)

    def test_negative_version(self):
        with self.assertRaises(ValueError):
            Observation("r", 1.0, 0, -1, STREAM_EDGE, STATUS_SUCCESS)

    def test_negative_latency(self):
        with self.assertRaises(ValueError):
            Observation("r", 1.0, 0, 1, STREAM_EDGE, STATUS_SUCCESS, latency_ms=-5)

    def test_non_finite_latency(self):
        with self.assertRaises(ValueError):
            Observation("r", 1.0, 0, 1, STREAM_EDGE, STATUS_SUCCESS, latency_ms=float("inf"))

    def test_rule_negative_allowed(self):
        with self.assertRaises(ValueError):
            _rule(allowed_latency_ms=-1)

    def test_rule_non_bool_receipt(self):
        with self.assertRaises(TypeError):
            _rule(requires_receipt=1)

    def test_adjudicator_zero_window(self):
        with self.assertRaises(ValueError):
            Adjudicator([_rule()], window_size_s=0, threshold="0.5")

    def test_adjudicator_no_rules(self):
        with self.assertRaises(ValueError):
            Adjudicator([], window_size_s=10, threshold="0.5")

    def test_adjudicator_bad_threshold(self):
        for bad in ("-0.1", "1.2"):
            with self.assertRaises(ValueError):
                Adjudicator([_rule()], window_size_s=10, threshold=bad)

    def test_ingest_wrong_type(self):
        adj = Adjudicator([_rule()], window_size_s=10, threshold="0.5")
        with self.assertRaises(TypeError):
            adj.ingest({"request_id": "r"})  # type: ignore[arg-type]


class OutOfOrderTests(unittest.TestCase):
    def test_interleaved_streams_dedup_regardless_of_arrival_order(self):
        base_obs = [
            Observation("r", 1.0, 0, 1, STREAM_EDGE, STATUS_FAILURE, observed_at=1.2),
            Observation("r", 1.0, 1, 2, STREAM_DOWNSTREAM, STATUS_SUCCESS,
                        latency_ms=200.0, receipt_present=False, observed_at=2.0),
            Observation("r", 1.0, 1, 3, STREAM_RECEIPT, STATUS_SUCCESS,
                        latency_ms=200.0, receipt_present=True, observed_at=2.5),
        ]
        outcomes = []
        for perm in itertools.permutations(base_obs):
            adj = Adjudicator([_rule(user_deadline_ms=100000.0)], window_size_s=10.0,
                              threshold="0.5", allowed_lateness_s=1000.0)
            for obs in perm:
                adj.ingest(obs)
            outcomes.append(adj.request_outcome("r"))
            self.assertEqual(adj.decision(0).total, 1)
        # Highest version (receipt) is authoritative in every ordering.
        self.assertTrue(all(o == OUTCOME_SUCCESS for o in outcomes))

    def test_out_of_order_observation_does_not_double_count(self):
        adj = Adjudicator([_rule(user_deadline_ms=100000.0)], window_size_s=10.0,
                          threshold="0.5", allowed_lateness_s=1000.0)
        # later-versioned record arrives first
        adj.ingest(Observation("r", 1.0, 1, 3, STREAM_RECEIPT, STATUS_SUCCESS,
                               latency_ms=200.0, receipt_present=True, observed_at=30.0))
        # earlier-versioned record arrives afterwards
        adj.ingest(Observation("r", 1.0, 0, 1, STREAM_EDGE, STATUS_FAILURE,
                               observed_at=1.2))
        dec = adj.decision(0)
        self.assertEqual(dec.total, 1)
        self.assertEqual(dec.success, 1)
        self.assertEqual(dec.failure, 0)


class InterleavingStressTests(unittest.TestCase):
    """Same window assembled from many ingestion orders must agree."""

    def _records(self):
        recs = []
        for i in range(20):
            rid = f"r{i}"
            entry = 1.0 + (i % 15) * 0.5  # spread within window [0, 10)
            # edge failure first
            recs.append(
                Observation(rid, entry, 0, 1, STREAM_EDGE, STATUS_FAILURE,
                            observed_at=entry + 0.1)
            )
            # retry partial success
            recs.append(
                Observation(rid, entry, 1, 2, STREAM_DOWNSTREAM, STATUS_SUCCESS,
                            latency_ms=150.0, receipt_present=False,
                            observed_at=entry + 0.2)
            )
            # some get a confirming receipt, some do not
            if i % 3 == 0:
                recs.append(
                    Observation(rid, entry, 1, 3, STREAM_RECEIPT, STATUS_SUCCESS,
                                latency_ms=150.0, receipt_present=True,
                                observed_at=entry + 0.3)
                )
        return recs

    def _build(self, order):
        adj = Adjudicator([_rule(user_deadline_ms=100000.0)], window_size_s=10.0,
                          threshold="0.5", allowed_lateness_s=0.0, auto_seal=False)
        for rec in order:
            adj.ingest(rec)
        return adj

    def test_order_invariant_final_counts(self):
        import random

        recs = self._records()
        baseline = self._build(recs)
        base = baseline.decision(0)
        self.assertEqual(base.total, 20)  # one sample per request despite ~53 records

        rng = random.Random(1234)
        for _ in range(25):
            shuffled = list(recs)
            rng.shuffle(shuffled)
            adj = self._build(shuffled)
            dec = adj.decision(0)
            self.assertEqual(dec.total, 20)
            self.assertEqual(
                (dec.success, dec.failure, dec.pending),
                (base.success, base.failure, base.pending),
            )

    def test_log_deltas_consistent_under_shuffle(self):
        import random

        recs = self._records()
        rng = random.Random(99)
        shuffled = list(recs)
        rng.shuffle(shuffled)
        adj = self._build(shuffled)
        win0 = [e for e in adj.log() if e.window_id == 0]
        dec = adj.decision(0)
        self.assertEqual(sum(e.delta_success for e in win0), dec.success)
        self.assertEqual(sum(e.delta_failure for e in win0), dec.failure)
        self.assertEqual(sum(e.delta_pending for e in win0), dec.pending)


class ReceiptVersionScopeTests(unittest.TestCase):
    """Receipt beats partial success *within* a version, not across versions."""

    def test_receipt_beats_partial_same_version(self):
        adj = Adjudicator([_rule(user_deadline_ms=100000.0)], window_size_s=10.0,
                          threshold="0.5", allowed_lateness_s=0.0, auto_seal=False)
        adj.ingest(Observation("r", 1.0, 0, 2, STREAM_DOWNSTREAM, STATUS_SUCCESS,
                               latency_ms=100.0, receipt_present=False, observed_at=1.1))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_PENDING)
        adj.ingest(Observation("r", 1.0, 0, 2, STREAM_RECEIPT, STATUS_SUCCESS,
                               latency_ms=100.0, receipt_present=True, observed_at=1.2))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_SUCCESS)

    def test_higher_version_partial_supersedes_lower_receipt(self):
        # The valid version is the newest one; within it the partial success is
        # what counts, so the sample stays pending until a receipt arrives.
        adj = Adjudicator([_rule(user_deadline_ms=100000.0)], window_size_s=10.0,
                          threshold="0.5", allowed_lateness_s=0.0, auto_seal=False)
        adj.ingest(Observation("r", 1.0, 0, 1, STREAM_RECEIPT, STATUS_SUCCESS,
                               latency_ms=100.0, receipt_present=True, observed_at=1.1))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_SUCCESS)
        adj.ingest(Observation("r", 1.0, 1, 2, STREAM_DOWNSTREAM, STATUS_SUCCESS,
                               latency_ms=100.0, receipt_present=False, observed_at=1.2))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_PENDING)


class VerificationOfCountsTests(unittest.TestCase):
    def test_counts_match_per_request_outcomes(self):
        adj = Adjudicator([_rule(user_deadline_ms=100000.0)], window_size_s=10.0,
                          threshold="0.5", allowed_lateness_s=1000.0)
        # 3 requests: success, failure, pending
        adj.ingest(Observation("s", 1.0, 0, 1, STREAM_RECEIPT, STATUS_SUCCESS,
                               latency_ms=100.0, receipt_present=True, observed_at=1.1))
        adj.ingest(Observation("f", 2.0, 0, 1, STREAM_EDGE, STATUS_FAILURE, observed_at=2.1))
        adj.ingest(Observation("p", 3.0, 0, 1, STREAM_DOWNSTREAM, STATUS_SUCCESS,
                               latency_ms=100.0, receipt_present=False, observed_at=3.1))
        dec = adj.decision(0)
        self.assertEqual((dec.success, dec.failure, dec.pending), (1, 1, 1))
        self.assertEqual(dec.total, 3)
        self.assertAlmostEqual(dec.lower_bound, 1 / 3)
        self.assertAlmostEqual(dec.upper_bound, 2 / 3)

    def test_log_deltas_sum_to_final_counts(self):
        adj = Adjudicator([_rule(user_deadline_ms=100000.0)], window_size_s=10.0,
                          threshold="0.5", allowed_lateness_s=1000.0)
        adj.ingest(Observation("a", 1.0, 0, 1, STREAM_RECEIPT, STATUS_SUCCESS,
                               latency_ms=100.0, receipt_present=True, observed_at=1.1))
        adj.ingest(Observation("b", 2.0, 0, 1, STREAM_EDGE, STATUS_FAILURE, observed_at=2.1))
        adj.ingest(Correction("b", 2.0, 0, 2, STATUS_SUCCESS, 90.0, True, observed_at=3.0))
        win0 = [e for e in adj.log() if e.window_id == 0]
        d_s = sum(e.delta_success for e in win0)
        d_f = sum(e.delta_failure for e in win0)
        d_p = sum(e.delta_pending for e in win0)
        dec = adj.decision(0)
        self.assertEqual((d_s, d_f, d_p), (dec.success, dec.failure, dec.pending))


if __name__ == "__main__":
    unittest.main()
