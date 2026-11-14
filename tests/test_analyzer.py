"""Unit tests for handshake_analyzer (stdlib unittest only)."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handshake_analyzer import (  # noqa: E402
    Policy,
    analyze_lines,
    format_ts,
    parse_ts,
    window_start,
)


def make_policy(min_samples=3, anomaly_ratio=0.25):
    return Policy.from_dict({
        "services": {
            "svc-a": {
                "fingerprints": [
                    {
                        "fingerprint": "fp-old",
                        "valid_from": "2026-11-14T00:00:00Z",
                        "valid_until": "2026-11-14T08:30:00Z",
                    },
                    {
                        "fingerprint": "fp-new",
                        "valid_from": "2026-11-14T08:00:00Z",
                        "valid_until": None,
                    },
                ],
                "min_samples": min_samples,
                "anomaly_ratio": anomaly_ratio,
            }
        }
    })


def rec(ts, service="svc-a", conn="c-1", result="success", fp="fp-new"):
    return json.dumps({
        "ts": ts,
        "service": service,
        "conn_id": conn,
        "result": result,
        "fingerprint": fp,
    })


class TestTimeHandling(unittest.TestCase):
    def test_window_alignment_floors_to_ten_minutes(self):
        self.assertEqual(
            format_ts(window_start(parse_ts("2026-11-14T08:29:59Z"))),
            "2026-11-14T08:20:00Z",
        )

    def test_window_alignment_exact_boundary_starts_new_window(self):
        self.assertEqual(
            format_ts(window_start(parse_ts("2026-11-14T08:30:00Z"))),
            "2026-11-14T08:30:00Z",
        )

    def test_parse_ts_requires_offset(self):
        with self.assertRaises(ValueError):
            parse_ts("2026-11-14 08:30:00")


class TestPolicyJudgment(unittest.TestCase):
    def test_rotation_overlap_accepts_old_and_new(self):
        policy = make_policy()
        lines = [
            rec("2026-11-14T08:10:00Z", conn="c-1", fp="fp-old"),
            rec("2026-11-14T08:11:00Z", conn="c-2", fp="fp-new"),
            rec("2026-11-14T08:12:00Z", conn="c-3", fp="fp-old"),
        ]
        report = analyze_lines(lines, policy)
        self.assertEqual(report["alerts"], [])
        self.assertEqual(report["evaluated_windows"][0]["anomalous"], 0)
        self.assertEqual(report["evaluated_windows"][0]["samples"], 3)

    def test_old_fingerprint_anomalous_after_valid_until(self):
        policy = make_policy()
        lines = [
            rec("2026-11-14T08:31:00Z", conn="c-1", fp="fp-new"),
            rec("2026-11-14T08:32:00Z", conn="c-2", fp="fp-new"),
            rec("2026-11-14T08:33:00Z", conn="c-3", fp="fp-old"),
        ]
        report = analyze_lines(lines, policy)
        self.assertEqual(report["evaluated_windows"][0]["anomalous"], 1)

    def test_valid_until_is_exclusive(self):
        policy = make_policy()
        lines = [
            rec("2026-11-14T08:29:59Z", conn="c-1", fp="fp-old"),
            rec("2026-11-14T08:30:00Z", conn="c-2", fp="fp-old"),
            rec("2026-11-14T08:30:01Z", conn="c-3", fp="fp-new"),
        ]
        report = analyze_lines(lines, policy)
        by_window = {w["window_start"]: w for w in report["evaluated_windows"]}
        self.assertEqual(by_window["2026-11-14T08:20:00Z"]["anomalous"], 0)
        self.assertEqual(by_window["2026-11-14T08:30:00Z"]["anomalous"], 1)

    def test_unknown_fingerprint_is_anomalous(self):
        policy = make_policy()
        lines = [
            rec("2026-11-14T08:10:00Z", conn="c-1", fp="fp-new"),
            rec("2026-11-14T08:11:00Z", conn="c-2", fp="fp-new"),
            rec("2026-11-14T08:12:00Z", conn="c-3", fp="fp-rogue"),
        ]
        report = analyze_lines(lines, policy)
        self.assertEqual(report["alerts"][0]["anomalous_fingerprints"],
                         [{"fingerprint": "fp-rogue", "count": 1}])


class TestDedupAndQuality(unittest.TestCase):
    def test_identical_reports_counted_once(self):
        policy = make_policy()
        line = rec("2026-11-14T08:10:00Z", conn="c-1", fp="fp-new")
        report = analyze_lines([line, line, line], policy)
        self.assertEqual(report["evaluated_windows"][0]["samples"], 1)
        self.assertEqual(report["quality"]["duplicate_reports"], 2)

    def test_conflicting_connection_excluded_and_reported(self):
        policy = make_policy()
        lines = [
            rec("2026-11-14T08:10:00Z", conn="c-9", fp="fp-old"),
            rec("2026-11-14T08:10:00Z", conn="c-9", fp="fp-new"),
        ]
        report = analyze_lines(lines, policy)
        self.assertEqual(report["evaluated_windows"], [])
        conflicts = report["quality"]["conflicting_connections"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["conn_id"], "c-9")
        self.assertEqual(len(conflicts[0]["variants"]), 2)

    def test_missing_service_or_fingerprint_reported(self):
        policy = make_policy()
        lines = [
            json.dumps({"ts": "2026-11-14T08:10:00Z", "conn_id": "c-1",
                        "result": "success", "fingerprint": "fp-new"}),
            json.dumps({"ts": "2026-11-14T08:10:00Z", "service": "svc-a",
                        "conn_id": "c-2", "result": "success"}),
        ]
        report = analyze_lines(lines, policy)
        missing = report["quality"]["missing_field_records"]
        self.assertEqual(len(missing), 2)
        self.assertEqual(missing[0]["missing"], ["service"])
        self.assertEqual(missing[1]["missing"], ["fingerprint"])
        self.assertEqual(report["evaluated_windows"], [])

    def test_failed_handshakes_not_in_denominator(self):
        policy = make_policy()
        lines = [
            rec("2026-11-14T08:10:00Z", conn="c-1", result="failure"),
            rec("2026-11-14T08:11:00Z", conn="c-2", result="failure"),
            rec("2026-11-14T08:12:00Z", conn="c-3"),
        ]
        report = analyze_lines(lines, policy)
        self.assertEqual(report["evaluated_windows"][0]["samples"], 1)
        self.assertEqual(report["quality"]["failed_handshakes"], 2)

    def test_unknown_service_is_unjudgeable(self):
        policy = make_policy()
        lines = [rec("2026-11-14T08:10:00Z", service="svc-ghost", conn="c-1")]
        report = analyze_lines(lines, policy)
        self.assertEqual(report["evaluated_windows"], [])
        self.assertEqual(len(report["quality"]["unjudgeable_records"]), 1)

    def test_parse_error_line_reported(self):
        policy = make_policy()
        report = analyze_lines(["{not valid json"], policy)
        self.assertEqual(report["quality"]["parse_error_lines"], [1])

    def test_invalid_timestamp_reported(self):
        policy = make_policy()
        lines = [rec("not-a-time", conn="c-1")]
        report = analyze_lines(lines, policy)
        self.assertEqual(len(report["quality"]["invalid_timestamp_records"]), 1)
        self.assertEqual(report["evaluated_windows"], [])


class TestAlertGating(unittest.TestCase):
    def test_min_samples_gate_blocks_alert(self):
        policy = make_policy(min_samples=5, anomaly_ratio=0.1)
        lines = [
            rec("2026-11-14T08:31:00Z", conn="c-1", fp="fp-new"),
            rec("2026-11-14T08:32:00Z", conn="c-2", fp="fp-old"),
        ]
        report = analyze_lines(lines, policy)
        self.assertEqual(report["alerts"], [])
        self.assertFalse(report["evaluated_windows"][0]["alerted"])

    def test_ratio_below_threshold_blocks_alert(self):
        policy = make_policy(min_samples=3, anomaly_ratio=0.5)
        lines = [
            rec("2026-11-14T08:31:00Z", conn="c-1", fp="fp-new"),
            rec("2026-11-14T08:32:00Z", conn="c-2", fp="fp-new"),
            rec("2026-11-14T08:33:00Z", conn="c-3", fp="fp-new"),
            rec("2026-11-14T08:34:00Z", conn="c-4", fp="fp-old"),
        ]
        report = analyze_lines(lines, policy)  # ratio 1/4 < 1/2
        self.assertEqual(report["alerts"], [])

    def test_ratio_exactly_at_threshold_alerts(self):
        policy = make_policy(min_samples=2, anomaly_ratio=0.5)
        lines = [
            rec("2026-11-14T08:31:00Z", conn="c-1", fp="fp-new"),
            rec("2026-11-14T08:32:00Z", conn="c-2", fp="fp-old"),
        ]
        report = analyze_lines(lines, policy)  # ratio 1/2 >= 1/2
        self.assertEqual(len(report["alerts"]), 1)

    def test_alert_lists_service_window_fingerprints_and_counts(self):
        policy = make_policy(min_samples=3, anomaly_ratio=0.25)
        lines = [
            rec("2026-11-14T08:31:00Z", conn="c-1", fp="fp-new"),
            rec("2026-11-14T08:32:00Z", conn="c-2", fp="fp-old"),
            rec("2026-11-14T08:33:00Z", conn="c-3", fp="fp-old"),
            rec("2026-11-14T08:34:00Z", conn="c-4", fp="fp-rogue"),
        ]
        report = analyze_lines(lines, policy)
        self.assertEqual(len(report["alerts"]), 1)
        alert = report["alerts"][0]
        self.assertEqual(alert["service"], "svc-a")
        self.assertEqual(alert["window_start"], "2026-11-14T08:30:00Z")
        self.assertEqual(alert["window_end"], "2026-11-14T08:40:00Z")
        self.assertEqual(alert["samples"], 4)
        self.assertEqual(alert["anomalous"], 3)
        self.assertEqual(
            alert["anomalous_fingerprints"],
            [
                {"fingerprint": "fp-old", "count": 2},
                {"fingerprint": "fp-rogue", "count": 1},
            ],
        )


if __name__ == "__main__":
    unittest.main()
