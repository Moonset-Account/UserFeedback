import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capacity_audit import analyze  # noqa: E402


class WindowBoundsTests(unittest.TestCase):
    def test_half_open_boundary_belongs_to_next_window(self):
        result = analyze(
            [
                {"event_id": "a", "kind": "arrival", "timestamp": 10.0,
                 "request_id": "r1"},
                {"event_id": "c", "kind": "completion", "timestamp": 20.0,
                 "request_id": "r1"},
            ],
            window_width=10,
            exhaustion_at=21,
        )
        self.assertEqual(result.window(0).arrivals.point, 0)
        self.assertEqual(result.window(1).arrivals.point, 1)
        self.assertEqual(result.window(1).completions.point, 0)
        self.assertEqual(result.window(2).completions.point, 1)

    def test_clock_uncertainty_gives_lower_point_upper(self):
        result = analyze(
            [
                {
                    "event_id": "c-near-boundary",
                    "kind": "completion",
                    "timestamp": 9.95,
                    "request_id": "r1",
                    # Ensure an arrival exists so reconciliation is realistic.
                },
                {"event_id": "a1", "kind": "arrival", "timestamp": 1.0,
                 "request_id": "r1"},
            ],
            window_width=10,
            default_clock_bounds=0.2,
            horizon=11,
        )
        w0 = result.window(0)
        w1 = result.window(1)
        # Corrected interval is [9.75, 10.15], crossing the boundary.
        self.assertEqual(w0.completions.lower, 0)
        self.assertEqual(w0.completions.point, 1)
        self.assertEqual(w0.completions.upper, 1)
        self.assertEqual(w1.completions.lower, 0)
        self.assertEqual(w1.completions.point, 0)
        self.assertEqual(w1.completions.upper, 1)
        self.assertAlmostEqual(w0.reported_throughput.lower, 0.0)
        self.assertAlmostEqual(w0.reported_throughput.point, 0.1)
        self.assertAlmostEqual(w0.reported_throughput.upper, 0.1)

    def test_certain_interior_event_counts_in_lower_and_upper(self):
        result = analyze(
            [{"event_id": "c", "kind": "completion", "timestamp": 5,
              "request_id": "r1"},
             {"event_id": "a", "kind": "arrival", "timestamp": 1,
              "request_id": "r1"}],
            window_width=10,
            default_clock_bounds=0.1,
            horizon=11,
        )
        bounds = result.window(0).completions
        self.assertEqual((bounds.lower, bounds.point, bounds.upper), (1, 1, 1))
        self.assertAlmostEqual(
            result.window(0).reported_throughput.point, 0.1
        )

    def test_missing_completion_reports_create_tracked_upper_ceiling(self):
        result = analyze(
            [
                {"event_id": "a1", "kind": "arrival", "timestamp": 1,
                 "request_id": "r1"},
                {"event_id": "a2", "kind": "arrival", "timestamp": 2,
                 "request_id": "r2"},
                {"event_id": "c1", "kind": "completion", "timestamp": 5,
                 "request_id": "r1"},
                # r2 completion report is missing; no exhaustion deadline.
            ],
            window_width=10,
            horizon=12,
        )
        w0 = result.window(0)
        self.assertEqual(w0.completions.point, 1)
        self.assertEqual(w0.missing_report_ceiling, 1)
        self.assertEqual(w0.throughput.lower, 0.1)
        self.assertEqual(w0.throughput.point, 0.1)
        # One observed completion plus one known unresolved request.
        self.assertEqual(w0.throughput.upper, 0.2)
        self.assertIn("r2", w0.potential_missing_completion_ids)


if __name__ == "__main__":
    unittest.main()
