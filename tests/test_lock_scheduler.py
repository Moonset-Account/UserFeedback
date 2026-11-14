"""Unit tests for the workspace lock scheduler.

Each test maps to an explicit requirement:
- cross-midnight shifts belong to the start date;
- half-open [start, end) periods;
- DST overlap -> earlier instant; DST gap -> shift forward to first valid time;
- temporary extensions are absolute UTC;
- per-workspace lock state, basis and next transition output;
- the two delivered timezone-boundary samples.
"""

import json
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lock_scheduler import evaluate, local_wall_to_utc, parse_utc  # noqa: E402

NY = ZoneInfo("America/New_York")
SAMPLES = ROOT / "samples"


def wall(text):
    return datetime.fromisoformat(text)


class TestDstConversion(unittest.TestCase):
    def test_overlap_picks_earlier_instant(self):
        # 2026-11-01 01:30 occurs twice in New York (EDT then EST).
        utc = local_wall_to_utc(wall("2026-11-01T01:30"), NY)
        self.assertEqual(utc, parse_utc("2026-11-01T05:30:00Z"))  # EDT, the earlier one

    def test_gap_shifts_forward_to_first_valid(self):
        # 2026-03-08 02:30 never happens in New York; first valid is 03:00 EDT.
        utc = local_wall_to_utc(wall("2026-03-08T02:30"), NY)
        self.assertEqual(utc, parse_utc("2026-03-08T07:00:00Z"))

    def test_gap_start_of_gap_also_shifts(self):
        utc = local_wall_to_utc(wall("2026-03-08T02:00"), NY)
        self.assertEqual(utc, parse_utc("2026-03-08T07:00:00Z"))

    def test_normal_time_unaffected(self):
        utc = local_wall_to_utc(wall("2026-06-15T09:30"), NY)
        self.assertEqual(utc, parse_utc("2026-06-15T13:30:00Z"))  # EDT = UTC-4


def base_config(**overrides):
    config = {
        "timezone": "America/New_York",
        "now_utc": "2026-10-27T07:00:00Z",
        "weekly_shifts": [{"weekday": "mon", "start": "22:00", "end": "06:00"}],
        "extensions": [],
        "workspaces": [{"id": "ws-a"}],
    }
    config.update(overrides)
    return config


class TestWeeklyShiftRules(unittest.TestCase):
    def test_cross_midnight_shift_attributed_to_start_date(self):
        # Monday 22:00-06:00 shift: Tuesday 03:00 local is still covered.
        report = evaluate(base_config(now_utc="2026-10-27T07:00:00Z"))  # Tue 03:00 EDT
        ws = report["workspaces"][0]
        self.assertFalse(ws["locked"])
        self.assertEqual(ws["basis"]["type"], "weekly_shift")
        self.assertEqual(ws["basis"]["weekday"], "mon")
        self.assertEqual(ws["basis"]["local_date"], "2026-10-26")

    def test_shift_not_active_on_wrong_weekday(self):
        # Wednesday 03:00 local: no Tuesday shift exists, so locked.
        report = evaluate(base_config(now_utc="2026-10-28T07:00:00Z"))
        ws = report["workspaces"][0]
        self.assertTrue(ws["locked"])
        self.assertEqual(ws["basis"]["type"], "none")

    def test_half_open_start_is_inclusive(self):
        report = evaluate(base_config(now_utc="2026-10-27T02:00:00Z"))  # Mon 22:00 EDT exactly
        self.assertFalse(report["workspaces"][0]["locked"])

    def test_half_open_end_is_exclusive(self):
        report = evaluate(base_config(now_utc="2026-10-27T10:00:00Z"))  # Tue 06:00 EDT exactly
        self.assertTrue(report["workspaces"][0]["locked"])

    def test_next_transition_when_unlocked_is_shift_end(self):
        report = evaluate(base_config(now_utc="2026-10-27T07:00:00Z"))
        ws = report["workspaces"][0]
        self.assertEqual(ws["next_transition_utc"], "2026-10-27T10:00:00Z")
        self.assertEqual(ws["next_state"], "locked")

    def test_next_transition_when_locked_is_next_shift_start(self):
        report = evaluate(base_config(now_utc="2026-10-28T07:00:00Z"))
        ws = report["workspaces"][0]
        # Next Monday 22:00 EDT = 2026-11-03T03:00:00Z (EST already: UTC-5 -> 03:00Z)
        self.assertEqual(ws["next_transition_utc"], "2026-11-03T03:00:00Z")
        self.assertEqual(ws["next_state"], "unlocked")

    def test_no_schedule_locks_forever(self):
        report = evaluate(base_config(weekly_shifts=[], extensions=[]))
        ws = report["workspaces"][0]
        self.assertTrue(ws["locked"])
        self.assertIsNone(ws["next_transition_utc"])
        self.assertIsNone(ws["next_state"])


class TestTemporaryExtension(unittest.TestCase):
    def test_extension_uses_absolute_utc(self):
        report = evaluate(
            base_config(
                weekly_shifts=[],
                extensions=[{
                    "start_utc": "2026-10-27T06:00:00Z",
                    "end_utc": "2026-10-27T08:00:00Z",
                    "label": "release-freeze-lift",
                }],
            )
        )
        ws = report["workspaces"][0]
        self.assertFalse(ws["locked"])
        self.assertEqual(ws["basis"]["type"], "temporary_extension")
        self.assertEqual(ws["basis"]["label"], "release-freeze-lift")
        self.assertEqual(ws["next_transition_utc"], "2026-10-27T08:00:00Z")

    def test_extension_end_is_exclusive(self):
        report = evaluate(
            base_config(
                weekly_shifts=[],
                extensions=[{
                    "start_utc": "2026-10-27T06:00:00Z",
                    "end_utc": "2026-10-27T07:00:00Z",
                }],
            )
        )
        self.assertTrue(report["workspaces"][0]["locked"])  # now == end exactly


class TestDeliveredSamples(unittest.TestCase):
    def _check(self, name):
        config = json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))
        expected = json.loads((SAMPLES / f"{name}.expected.json").read_text(encoding="utf-8"))
        actual = evaluate(config)
        self.assertEqual(actual, expected, f"sample {name} mismatch")

    def test_sample_dst_overlap(self):
        self._check("case_dst_overlap")

    def test_sample_dst_gap(self):
        self._check("case_dst_gap")


class TestCli(unittest.TestCase):
    def test_cli_matches_expected_sample(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "app.py"), str(SAMPLES / "case_dst_overlap.json")],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        actual = json.loads(proc.stdout)
        expected = json.loads((SAMPLES / "case_dst_overlap.expected.json").read_text(encoding="utf-8"))
        self.assertEqual(actual, expected)

    def test_cli_rejects_missing_args(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "app.py")],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
