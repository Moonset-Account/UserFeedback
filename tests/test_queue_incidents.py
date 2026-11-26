import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capacity_audit import analyze  # noqa: E402


class QueueIntervalAndIncidentTests(unittest.TestCase):
    def test_completed_interval_has_explainable_duration_bounds(self):
        result = analyze(
            [
                {"event_id": "a", "kind": "arrival", "timestamp": 1.0,
                 "request_id": "r1", "clock_error": 0.1},
                {"event_id": "c", "kind": "completion", "timestamp": 5.0,
                 "request_id": "r1", "clock_error": 0.2},
            ],
            window_width=10,
            horizon=10,
        )
        interval = next(i for i in result.intervals if i.subject_type == "request")
        self.assertEqual(interval.status, "completed")
        self.assertTrue(interval.closed)
        self.assertFalse(interval.inferred)
        # Earliest duration: completion earliest 4.8 - arrival latest 1.1 = 3.7.
        # Latest duration: completion latest 5.2 - arrival earliest .9 = 4.3.
        self.assertAlmostEqual(interval.duration_bounds.lower, 3.7, places=7)
        self.assertAlmostEqual(interval.duration_bounds.upper, 4.3, places=7)
        self.assertEqual(interval.start_window.point_index, 0)
        self.assertEqual(interval.end_window.point_index, 0)

    def test_active_withdrawal_is_located_and_not_capacity_loss(self):
        result = analyze(
            [
                {"event_id": "a", "kind": "arrival", "timestamp": 2,
                 "request_id": "r1", "client_id": "c1"},
                {"event_id": "w", "kind": "withdrawal", "timestamp": 7,
                 "request_id": "r1", "client_id": "c1", "reason": "client timeout"},
            ],
            window_width=10,
            exhaustion_at=20,
        )
        self.assertEqual(len(result.withdrawals), 1)
        self.assertEqual(result.capacity_losses, [])
        incident = result.withdrawals[0]
        self.assertTrue(incident.counts_as_withdrawal)
        self.assertFalse(incident.counts_as_capacity_loss)
        self.assertEqual(incident.point_window_index, 0)
        self.assertEqual(incident.client_id, "c1")
        self.assertEqual(result.window(0).withdrawals.point, 1)
        self.assertEqual(result.window(1).capacity_lost.point, 0)
        interval = next(i for i in result.intervals if i.request_id == "r1")
        self.assertEqual(interval.status, "withdrawn")

    def test_unresolved_known_request_at_exhaustion_is_capacity_loss(self):
        result = analyze(
            [
                {"event_id": "a", "kind": "arrival", "timestamp": 3,
                 "request_id": "lost-1"},
                {"event_id": "dup", "kind": "arrival", "timestamp": 3,
                 "request_id": "lost-1"},
            ],
            window_width=10,
            exhaustion_at=12,
        )
        self.assertEqual(len(result.capacity_losses), 1)
        loss = result.capacity_losses[0]
        self.assertEqual(loss.count, 1)
        self.assertEqual(loss.point_window_index, 1)  # time 12 is in [10,20)
        self.assertEqual(loss.request_id, "lost-1")
        self.assertEqual(loss.detected_by, "request_mass_balance")
        self.assertTrue(loss.counts_as_capacity_loss)
        interval = next(i for i in result.intervals if i.request_id == "lost-1")
        self.assertEqual(interval.status, "capacity_lost")
        self.assertTrue(interval.inferred)
        self.assertEqual(result.window(1).capacity_lost.point, 1)
        self.assertEqual(result.summary["capacity_lost_count"], 1)

    def test_open_at_horizon_without_exhaustion_is_not_called_loss(self):
        result = analyze(
            [{"event_id": "a", "kind": "arrival", "timestamp": 3,
              "request_id": "open-1"}],
            window_width=10,
            horizon=9,
        )
        self.assertEqual(result.capacity_losses, [])
        interval = next(i for i in result.intervals if i.request_id == "open-1")
        self.assertEqual(interval.status, "open_observed_to_horizon")
        self.assertFalse(interval.closed)

    def test_aggregate_batch_balance_locates_unnamed_capacity_loss(self):
        result = analyze(
            [
                {"event_id": "arr-batch", "kind": "arrival", "timestamp": 1,
                 "lineage_id": "L-batch", "count": 4},
                {"event_id": "comp-batch", "kind": "completion", "timestamp": 5,
                 "lineage_id": "L-batch", "count": 2},
            ],
            window_width=10,
            exhaustion_at=15,
        )
        cohort = result.cohorts["L-batch"]
        self.assertEqual(cohort.arrival_count, 4)
        self.assertEqual(cohort.completed_count, 2)
        self.assertEqual(cohort.open_count, 2)
        aggregate_losses = [
            x for x in result.capacity_losses
            if x.detected_by == "cohort_mass_balance"
        ]
        self.assertEqual(len(aggregate_losses), 1)
        self.assertEqual(aggregate_losses[0].count, 2)
        self.assertEqual(aggregate_losses[0].point_window_index, 1)
        self.assertEqual(result.window(1).capacity_lost.point, 2)

    def test_rate_limit_without_reissue_is_admission_gap_not_capacity_loss(self):
        result = analyze(
            [
                {"event_id": "a", "kind": "arrival", "timestamp": 1,
                 "request_id": "throttled-1"},
                {"event_id": "limit", "kind": "rate_limited", "timestamp": 8,
                 "request_id": "throttled-1"},
            ],
            window_width=10,
            exhaustion_at=25,
        )
        self.assertEqual(result.capacity_losses, [])
        self.assertEqual(len(result.admission_gaps), 1)
        gap = result.admission_gaps[0]
        self.assertEqual(gap.request_id, "throttled-1")
        self.assertEqual(gap.point_window_index, 0)  # located at rate limit t=8
        self.assertTrue(gap.counts_as_admission_gap)
        self.assertFalse(gap.counts_as_capacity_loss)
        self.assertEqual(result.window(0).admission_gaps.point, 1)
        interval = next(i for i in result.intervals if i.request_id == "throttled-1")
        self.assertEqual(interval.status, "rate_limited_unrecovered")
        self.assertEqual(interval.end_event_id, "limit")

    def test_reissued_then_unresolved_is_still_capacity_loss(self):
        result = analyze(
            [
                {"event_id": "a", "kind": "arrival", "timestamp": 1,
                 "request_id": "r"},
                {"event_id": "limit", "kind": "rate_limited", "timestamp": 2,
                 "request_id": "r"},
                {"event_id": "rr", "kind": "reissue", "timestamp": 11,
                 "request_id": "r"},
            ],
            window_width=10,
            exhaustion_at=25,
        )
        self.assertEqual(result.admission_gaps, [])
        self.assertEqual(len(result.capacity_losses), 1)
        self.assertEqual(result.capacity_losses[0].point_window_index, 2)

    def test_explicit_capacity_loss_event_is_located(self):
        result = analyze(
            [
                {"event_id": "a", "kind": "arrival", "timestamp": 1,
                 "lineage_id": "L", "count": 2},
                {"event_id": "drop", "kind": "capacity_loss", "timestamp": 9,
                 "lineage_id": "L", "count": 2, "reason": "queue full"},
            ],
            window_width=10,
            exhaustion_at=20,
        )
        self.assertEqual(len(result.capacity_losses), 1)
        self.assertEqual(result.capacity_losses[0].detected_by, "event")
        self.assertEqual(result.window(0).capacity_lost.point, 2)
        self.assertEqual(result.summary["capacity_lost_count"], 2)

    def test_conflicting_terminal_reports_keep_window_upper_bounds(self):
        result = analyze(
            [
                {"event_id": "a", "kind": "arrival", "timestamp": 1,
                 "request_id": "conflict-1"},
                {"event_id": "c", "kind": "completion", "timestamp": 5,
                 "request_id": "conflict-1"},
                {"event_id": "w", "kind": "withdrawal", "timestamp": 6,
                 "request_id": "conflict-1"},
            ],
            window_width=10,
            exhaustion_at=20,
        )
        w0 = result.window(0)
        # Conflict means neither terminal is certain/point-booked, but both
        # outcomes remain possible in the upper bound.
        self.assertEqual(w0.completions.lower, 0)
        self.assertEqual(w0.completions.point, 0)
        self.assertEqual(w0.completions.upper, 1)
        self.assertEqual(w0.withdrawals.upper, 1)
        self.assertTrue(any(i.code == "terminal_conflict" for i in result.issues))
        self.assertEqual(len(result.withdrawals), 1)

    def test_queue_exhausted_event_can_supply_settlement_boundary(self):
        result = analyze(
            [
                {"event_id": "a", "kind": "arrival", "timestamp": 3,
                 "request_id": "r"},
                {"event_id": "exh", "kind": "queue_exhausted", "timestamp": 12},
            ],
            window_width=10,
        )
        self.assertEqual(result.exhaustion_at, 12)
        self.assertEqual(len(result.capacity_losses), 1)
        self.assertEqual(result.window(1).queue_exhausted.point, 1)

    def test_missing_original_arrival_is_flagged(self):
        result = analyze(
            [{"event_id": "c-orphan", "kind": "completion", "timestamp": 4,
              "request_id": "orphan"}],
            window_width=10,
            exhaustion_at=20,
        )
        interval = next(i for i in result.intervals if i.request_id == "orphan")
        self.assertEqual(interval.status, "missing_arrival_evidence")
        self.assertTrue(any(i.code == "missing_arrival_evidence" for i in result.issues)
                        or result.cohorts[interval.lineage_id].missing_arrival_count == 1)


if __name__ == "__main__":
    unittest.main()
