import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capacity_audit import analyze  # noqa: E402


class ReissueAttributionTests(unittest.TestCase):
    def test_reissue_in_next_window_is_not_new_demand(self):
        result = analyze(
            [
                {
                    "event_id": "arr-1",
                    "kind": "arrival",
                    "timestamp": 9.2,
                    "count": 4,
                },
                {
                    "event_id": "limit-1",
                    "kind": "rate_limit",
                    "timestamp": 9.6,
                    "source_arrival_id": "arr-1",
                    "count": 4,
                },
                {
                    "event_id": "reissue-1",
                    "kind": "reissue",
                    "timestamp": 10.4,
                    "source_arrival_id": "arr-1",
                    "batch_id": "recovery-1",
                    "count": 4,
                },
                {
                    "event_id": "complete-1",
                    "kind": "completion",
                    "timestamp": 14.0,
                    "source_arrival_id": "arr-1",
                    "count": 3,
                },
                {
                    "event_id": "withdraw-1",
                    "kind": "withdrawal",
                    "timestamp": 15.0,
                    "source_arrival_id": "arr-1",
                    "count": 1,
                },
            ],
            window_width=10,
            exhaustion_at=30,
        )

        w0 = result.window(0)
        w1 = result.window(1)
        self.assertEqual(w0.arrivals.point, 4)
        self.assertEqual(w0.rate_limited.point, 4)
        self.assertEqual(w0.reissues.point, 0)

        self.assertEqual(w1.arrivals.point, 0)
        self.assertEqual(w1.reissues.point, 4)
        self.assertEqual(w1.completions.point, 3)
        self.assertEqual(w1.withdrawals.point, 1)

        self.assertEqual(result.summary["unique_original_arrivals"], 4)
        self.assertEqual(result.summary["physical_reissue_count"], 4)
        # Not 4 arrivals + 4 reissues of "new demand".
        self.assertEqual(result.summary["withdrawn_count"], 1)
        self.assertEqual(result.capacity_losses, [])

        self.assertEqual(len(result.reissue_attributions), 1)
        attribution = result.reissue_attributions[0]
        self.assertEqual(attribution.original_window_index, 0)
        self.assertEqual(attribution.reissue_window.point_index, 1)
        self.assertEqual(attribution.original_window_certain, 0)
        self.assertEqual(attribution.reissue_window.certain_index, 1)
        self.assertTrue(attribution.crosses_point_window)
        self.assertEqual(attribution.naive_new_arrival_overcount, 4)

        zero_demand_entries = [
            e for e in attribution.entries if e.metric == "new_unique_demand"
        ]
        self.assertEqual(len(zero_demand_entries), 1)
        self.assertEqual(zero_demand_entries[0].count, 0)
        self.assertEqual(zero_demand_entries[0].window_index, 1)

    def test_split_recovery_batches_are_counted_in_their_send_windows(self):
        result = analyze(
            [
                {"event_id": "arr", "kind": "arrival", "timestamp": 8, "count": 4},
                {"event_id": "rr-a", "kind": "reissue", "timestamp": 11,
                 "source_arrival_id": "arr", "batch_id": "A", "count": 2},
                {"event_id": "rr-b", "kind": "reissue", "timestamp": 21,
                 "source_arrival_id": "arr", "batch_id": "B", "count": 2},
                {"event_id": "c-a", "kind": "completion", "timestamp": 12,
                 "source_arrival_id": "arr", "batch_id": "A", "count": 2},
                {"event_id": "c-b", "kind": "completion", "timestamp": 22,
                 "source_arrival_id": "arr", "batch_id": "B", "count": 2},
            ],
            window_width=10,
            exhaustion_at=35,
        )
        self.assertEqual(result.window(0).arrivals.point, 4)
        self.assertEqual(result.window(1).reissues.point, 2)
        self.assertEqual(result.window(2).reissues.point, 2)
        self.assertEqual(result.window(1).completions.point, 2)
        self.assertEqual(result.window(2).completions.point, 2)
        self.assertEqual(len(result.cross_window_reissues), 2)
        self.assertEqual(result.summary["capacity_lost_count"], 0)

    def test_clock_skew_can_make_reissue_window_uncertain(self):
        result = analyze(
            [
                {"event_id": "arr", "kind": "arrival", "timestamp": 8,
                 "request_id": "r1", "clock_error": 0.1},
                {
                    "event_id": "rr",
                    "kind": "reissue",
                    "timestamp": 10.05,
                    "request_id": "r1",
                    "count": 2,
                    "clock_error": 0.2,
                },
            ],
            window_width=10,
            horizon=12,
        )
        w0 = result.window(0)
        w1 = result.window(1)
        self.assertEqual(w0.reissues.lower, 0)
        self.assertEqual(w0.reissues.point, 0)
        self.assertEqual(w0.reissues.upper, 2)
        self.assertEqual(w1.reissues.lower, 0)
        self.assertEqual(w1.reissues.point, 2)
        self.assertEqual(w1.reissues.upper, 2)

        attribution = result.reissue_attributions[0]
        self.assertIsNone(attribution.reissue_window.certain_index)
        self.assertEqual(attribution.reissue_window.possible_indices, [0, 1])
        self.assertEqual(attribution.reissue_window.point_index, 1)
        self.assertTrue(attribution.crosses_point_window)


if __name__ == "__main__":
    unittest.main()
