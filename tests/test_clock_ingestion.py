import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capacity_audit import (  # noqa: E402
    ClockSpec,
    Event,
    ManualClock,
    ingest_records,
)


class ClockTests(unittest.TestCase):
    def test_manual_clock_is_controllable(self):
        clock = ManualClock(100.0)
        self.assertEqual(clock.now, 100.0)
        clock.advance(2.5)
        self.assertEqual(clock.now, 102.5)
        clock.sleep(1.0)
        self.assertEqual(clock.time(), 103.5)
        clock.set(0.0)
        self.assertEqual(clock.stamp(), 0.0)

    def test_clock_correction_and_error_bounds(self):
        # Client clock is 1 second behind reference and has 0.2 error.
        bounds = ClockSpec(offset=1.0, error_bound=0.2).correct(9.0)
        self.assertAlmostEqual(bounds.point, 10.0)
        self.assertAlmostEqual(bounds.lower, 9.8)
        self.assertAlmostEqual(bounds.upper, 10.2)
        self.assertAlmostEqual(bounds.width, 0.4, places=7)

    def test_negative_advance_rejected(self):
        clock = ManualClock()
        with self.assertRaises(ValueError):
            clock.advance(-1)


class IngestionTests(unittest.TestCase):
    def test_same_event_id_and_identical_payload_are_deduplicated(self):
        records = [
            {"event_id": "e1", "type": "arrival", "ts": 1.0, "request_id": "r1"},
            # Exact repeated report.
            {"event_id": "e1", "type": "arrival", "ts": 1.0, "request_id": "r1"},
            # New report id, same logical payload.
            {"event_id": "e1-retry-report", "kind": "arrival", "timestamp": 1.0,
             "request_id": "r1"},
        ]
        result = ingest_records(records)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(len(result.duplicates), 2)
        self.assertEqual(result.events[0].kind, "arrival")

    def test_invalid_record_is_issue_but_other_events_survive(self):
        result = ingest_records([
            {"kind": "not-a-real-kind", "timestamp": 1},
            {"kind": "arrival", "timestamp": 2, "request_id": "ok"},
        ])
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].request_id, "ok")
        self.assertTrue(any(i.code == "invalid_event" for i in result.issues))

    def test_sequence_gap_is_reported_not_filled(self):
        events = [
            Event(kind="arrival", timestamp=1, client_id="c", seq=1),
            Event(kind="arrival", timestamp=2, client_id="c", seq=2),
            Event(kind="completion", timestamp=3, client_id="c", seq=5),
        ]
        result = ingest_records(events)
        self.assertEqual(len(result.gaps), 1)
        gap = result.gaps[0]
        self.assertEqual((gap.before_seq, gap.after_seq), (2, 5))
        self.assertEqual(gap.missing_event_reports, 2)
        self.assertTrue(any(i.code == "sequence_gap" for i in result.issues))


if __name__ == "__main__":
    unittest.main()
