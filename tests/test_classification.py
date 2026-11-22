"""Tests for success/failure/pending classification against the entry rule."""

import unittest

from sli_adjudicator import (
    OUTCOME_FAILURE,
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


def _make(allowed=500.0, receipt=True, deadline=5000.0, threshold="0.5"):
    rule = Rule(
        effective_from=0.0,
        allowed_latency_ms=allowed,
        requires_receipt=receipt,
        user_deadline_ms=deadline,
    )
    return Adjudicator([rule], window_size_s=10.0, threshold=threshold)


def _req(rid, status=STATUS_SUCCESS, latency=100.0, receipt=True, observed=1.1,
         stream=STREAM_RECEIPT, attempt=0, version=1, entry=1.0):
    return Observation(
        request_id=rid,
        entry_time=entry,
        attempt=attempt,
        version=version,
        stream=stream,
        status=status,
        latency_ms=latency,
        receipt_present=receipt,
        observed_at=observed,
    )


class LatencyClassificationTests(unittest.TestCase):
    def test_within_allowed_is_success(self):
        adj = _make(allowed=500.0)
        adj.ingest(_req("r", latency=500.0))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_SUCCESS)

    def test_over_allowed_is_failure(self):
        adj = _make(allowed=500.0)
        adj.ingest(_req("r", latency=500.001))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_FAILURE)

    def test_latency_from_elapsed_when_none(self):
        adj = _make(allowed=500.0)
        # entry 1.0, observed 1.6 -> 600 ms elapsed -> over allowed.
        adj.ingest(_req("r", latency=None, observed=1.6))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_FAILURE)

    def test_explicit_latency_preferred_over_elapsed(self):
        adj = _make(allowed=500.0)
        # elapsed 3s but explicit latency 100ms -> success.
        adj.ingest(_req("r", latency=100.0, observed=4.0))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_SUCCESS)


class ReceiptClassificationTests(unittest.TestCase):
    def test_missing_receipt_is_pending_when_required(self):
        adj = _make(receipt=True)
        adj.ingest(_req("r", receipt=False, stream=STREAM_DOWNSTREAM))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_PENDING)

    def test_receipt_stream_implies_receipt(self):
        adj = _make(receipt=True)
        adj.ingest(_req("r", receipt=False, stream=STREAM_RECEIPT))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_SUCCESS)

    def test_receipt_not_required(self):
        adj = _make(receipt=False)
        adj.ingest(_req("r", receipt=False, stream=STREAM_DOWNSTREAM))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_SUCCESS)

    def test_failure_short_circuits_receipt(self):
        # A failure is a failure even without a receipt.
        adj = _make(receipt=True)
        adj.ingest(_req("r", status=STATUS_FAILURE, receipt=False, stream=STREAM_EDGE))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_FAILURE)


    def test_correction_latency_from_result_at_not_arrival(self):
        # A correction arrives very late (observed_at=50) but states the result
        # became true at result_at=1.2 -> latency 200ms, within the 500ms rule.
        adj = _make(allowed=500.0, deadline=100000.0)
        adj.ingest(_req("r", status=STATUS_FAILURE, observed=1.2))
        adj.ingest(
            Correction(
                request_id="r",
                entry_time=1.0,
                attempt=1,
                version=2,
                status=STATUS_SUCCESS,
                latency_ms=None,
                receipt_present=True,
                observed_at=50.0,
                result_at=1.2,
            )
        )
        self.assertEqual(adj.request_outcome("r"), OUTCOME_SUCCESS)


class DeadlineClassificationTests(unittest.TestCase):
    def test_observation_after_deadline_is_pending(self):
        adj = _make(deadline=1000.0)  # 1 second deadline
        # entry 1.0, observed 3.0 -> 2s > 1s deadline, even though latency says 100
        adj.ingest(_req("r", latency=100.0, observed=3.0))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_PENDING)

    def test_observation_exactly_at_deadline_is_decidable(self):
        adj = _make(deadline=1000.0)
        adj.ingest(_req("r", latency=100.0, observed=2.0))  # exactly 1s
        self.assertEqual(adj.request_outcome("r"), OUTCOME_SUCCESS)

    def test_failure_reported_after_deadline_is_pending(self):
        # A request whose only sighting (here a failure) arrives after the user
        # deadline has no confirmable result in time -> pending, not failure.
        adj = _make(deadline=1000.0)
        adj.ingest(_req("r", status=STATUS_FAILURE, observed=5.0))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_PENDING)


class CorrectionClassificationTests(unittest.TestCase):
    def test_late_correction_applied_when_no_result_at(self):
        adj = _make(deadline=1000.0)
        adj.ingest(_req("r", status=STATUS_FAILURE, observed=1.2))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_FAILURE)
        # Correction arrives late but states no result_at -> authoritative.
        adj.ingest(
            Correction(
                request_id="r",
                entry_time=1.0,
                attempt=1,
                version=2,
                status=STATUS_SUCCESS,
                latency_ms=80.0,
                receipt_present=True,
                observed_at=8.0,
            )
        )
        self.assertEqual(adj.request_outcome("r"), OUTCOME_SUCCESS)

    def test_correction_result_at_after_deadline_is_pending(self):
        adj = _make(deadline=1000.0)
        adj.ingest(_req("r", status=STATUS_FAILURE, observed=1.2))
        adj.ingest(
            Correction(
                request_id="r",
                entry_time=1.0,
                attempt=1,
                version=2,
                status=STATUS_SUCCESS,
                latency_ms=80.0,
                receipt_present=True,
                observed_at=8.0,
                result_at=5.0,  # truth only confirmable 4s after entry
            )
        )
        self.assertEqual(adj.request_outcome("r"), OUTCOME_PENDING)

    def test_higher_version_correction_wins(self):
        adj = _make(deadline=10000.0)
        adj.ingest(_req("r", status=STATUS_FAILURE, observed=1.2))
        adj.ingest(
            Correction("r", 1.0, 1, 2, STATUS_SUCCESS, 80.0, True, observed_at=5.0)
        )
        self.assertEqual(adj.request_outcome("r"), OUTCOME_SUCCESS)
        adj.ingest(
            Correction("r", 1.0, 2, 3, STATUS_FAILURE, 80.0, True, observed_at=6.0)
        )
        self.assertEqual(adj.request_outcome("r"), OUTCOME_FAILURE)

    def test_correction_beats_observation_same_version(self):
        adj = _make(deadline=10000.0)
        adj.ingest(_req("r", status=STATUS_FAILURE, version=1, observed=1.2))
        adj.ingest(Correction("r", 1.0, 0, 1, STATUS_SUCCESS, 90.0, True, observed_at=3.0))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_SUCCESS)

    def test_partial_success_without_receipt_after_correction_failure(self):
        adj = _make(receipt=True, deadline=10000.0)
        adj.ingest(_req("r", status=STATUS_SUCCESS, receipt=False, stream=STREAM_EDGE))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_PENDING)
        adj.ingest(Correction("r", 1.0, 0, 2, STATUS_FAILURE, 90.0, True, observed_at=3.0))
        self.assertEqual(adj.request_outcome("r"), OUTCOME_FAILURE)


if __name__ == "__main__":
    unittest.main()
