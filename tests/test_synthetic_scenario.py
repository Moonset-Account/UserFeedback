import json
import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capacity_audit import ClockSpec, ManualClock, analyze  # noqa: E402


class SyntheticInterleavingTests(unittest.TestCase):
    def build_events(self):
        clock = ManualClock()
        offsets = {"A": 0.2, "B": -0.1}
        sequences = {"A": 0, "B": 0}
        events = []

        def emit(true_time, client, kind, **kwargs):
            clock.advance(true_time - clock.now)
            sequences[client] += 1
            event = {
                "event_id": kwargs.pop("event_id", f"{client}-{sequences[client]}-{kind}"),
                "kind": kind,
                "timestamp": true_time - offsets[client],
                "client_id": client,
                "seq": kwargs.pop("seq", sequences[client]),
                "true_timestamp": true_time,
                **kwargs,
            }
            events.append(event)
            return event

        # Successful request.
        emit(1.0, "A", "arrival", event_id="a1-arr", request_id="a1")

        # Active withdrawal interleaved with successful traffic.
        emit(2.0, "B", "arrival", event_id="b1-arr", request_id="b1", seq=1)

        # Known request with neither completion nor withdrawal: capacity loss
        # after the exhaustion boundary.
        emit(2.5, "B", "arrival", event_id="y1-arr", request_id="y1")

        # Aggregate batch: one completion, two unresolved capacities.
        emit(3.0, "A", "arrival", event_id="z-arr", lineage_id="Z", count=3)

        completion = emit(4.0, "A", "completion", event_id="a1-done",
                          request_id="a1")
        # Exact repeated report, including same event id.
        events.append(dict(completion))

        # Deliberate sequence jump: reports seq=4 means two missing reports.
        emit(6.0, "B", "withdrawal", event_id="b1-cancel",
             request_id="b1", seq=4, reason="client_deadline")

        emit(8.0, "A", "completion", event_id="z-one-done",
             lineage_id="Z", count=1)

        # A batch is throttled, backlogged, and recovered in window 1.
        emit(9.0, "A", "arrival", event_id="x-arr", lineage_id="X",
             count=2, request_ids=["x1", "x2"])
        emit(9.4, "A", "rate_limited", event_id="x-limit", lineage_id="X",
             count=2)
        emit(10.6, "A", "reissue", event_id="x-reissue", lineage_id="X",
             batch_id="recovery-X", count=2, request_ids=["x1", "x2"])
        emit(14.0, "A", "completion", event_id="x1-done", lineage_id="X",
             request_id="x1")
        emit(16.0, "A", "withdrawal", event_id="x2-cancel", lineage_id="X",
             request_id="x2")

        clock.advance(25.0 - clock.now)
        return events, clock

    def test_full_synthetic_ledger_and_interleaving(self):
        events, clock = self.build_events()
        result = analyze(
            events,
            window_width=10,
            clock=clock,
            exhaustion_at=clock.now,
            clock_bounds={
                "A": ClockSpec(offset=0.2, error_bound=0.1),
                "B": {"offset": -0.1, "error_bound": 0.15},
            },
        )

        # No synthetic true timestamp lies outside its client's error interval.
        self.assertFalse(
            any(i.code == "clock_bound_violation" for i in result.issues),
            [i.to_dict() for i in result.issues],
        )

        w0, w1, w2 = result.window(0), result.window(1), result.window(2)
        self.assertEqual(w0.arrivals.point, 8)
        self.assertEqual(w0.rate_limited.point, 2)
        self.assertEqual(w0.completions.point, 2)
        self.assertEqual(w0.withdrawals.point, 1)
        self.assertEqual(w0.reissues.point, 0)

        self.assertEqual(w1.arrivals.point, 0)
        self.assertEqual(w1.reissues.point, 2)
        self.assertEqual(w1.completions.point, 1)
        self.assertEqual(w1.withdrawals.point, 1)

        self.assertEqual(w2.capacity_lost.lower, 3)
        self.assertEqual(w2.capacity_lost.point, 3)
        self.assertEqual(w2.capacity_lost.upper, 3)

        summary = result.summary
        self.assertEqual(summary["unique_original_arrivals"], 8)
        self.assertEqual(summary["physical_reissue_count"], 2)
        self.assertEqual(summary["completed_count"], 3)
        self.assertEqual(summary["withdrawn_count"], 2)
        self.assertEqual(summary["capacity_lost_count"], 3)
        self.assertEqual(summary["inferred_capacity_lost_count"], 3)
        self.assertEqual(summary["duplicate_event_reports"], 1)
        self.assertEqual(summary["sequence_gap_count"], 1)
        self.assertEqual(summary["cross_window_reissue_count"], 1)

        # Withdrawals and losses are separate, located incidents.
        withdrawal_ids = {x.request_id for x in result.withdrawals}
        self.assertEqual(withdrawal_ids, {"b1", "x2"})
        self.assertTrue(all(x.counts_as_withdrawal for x in result.withdrawals))
        self.assertTrue(all(not x.counts_as_capacity_loss for x in result.withdrawals))
        loss_requests = {x.request_id for x in result.capacity_losses if x.request_id}
        self.assertEqual(loss_requests, {"y1"})
        aggregate_loss = next(
            x for x in result.capacity_losses
            if x.detected_by == "cohort_mass_balance"
        )
        self.assertEqual(aggregate_loss.lineage_id, "Z")
        self.assertEqual(aggregate_loss.count, 2)

        # Queue intervals cover both normal completion and inferred loss.
        request_statuses = {
            i.request_id: i.status
            for i in result.intervals
            if i.subject_type == "request"
        }
        self.assertEqual(request_statuses["a1"], "completed")
        self.assertEqual(request_statuses["b1"], "withdrawn")
        self.assertEqual(request_statuses["x1"], "completed")
        self.assertEqual(request_statuses["x2"], "withdrawn")
        self.assertEqual(request_statuses["y1"], "capacity_lost")

        # Result is JSON serializable for operational reporting.
        json.dumps(result.to_dict())

    def test_cross_window_journal_has_distinct_original_and_reissue_views(self):
        events, clock = self.build_events()
        result = analyze(
            events,
            window_width=10,
            clock=clock,
            exhaustion_at=clock.now,
            clock_bounds={
                "A": ClockSpec(offset=0.2, error_bound=0.1),
                "B": ClockSpec(offset=-0.1, error_bound=0.15),
            },
        )
        attribution = result.cross_window_reissues[0]
        self.assertEqual(attribution.lineage_id, "X")
        self.assertEqual(attribution.original_window_index, 0)
        self.assertEqual(attribution.reissue_window.point_index, 1)

        original_roles = [
            e for e in attribution.entries if e.window_index == 0
        ]
        reissue_roles = [
            e for e in attribution.entries if e.window_index == 1
        ]
        self.assertTrue(any(e.metric == "arrivals" and e.count == 2
                            for e in original_roles))
        self.assertTrue(any(e.metric == "backlog_to_be_recovered" and e.count == 2
                            for e in original_roles))
        self.assertTrue(any(e.metric == "reissues" and e.count == 2
                            for e in reissue_roles))
        self.assertTrue(any(e.metric == "new_unique_demand" and e.count == 0
                            for e in reissue_roles))


if __name__ == "__main__":
    unittest.main()
