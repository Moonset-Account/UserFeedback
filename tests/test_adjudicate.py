"""Certificate decisions derived from retained instants only."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from handshake_time import Verdict, adjudicate, covers

from tests.fixtures import BASE, receipt, utc

REPO_ROOT = Path(__file__).resolve().parents[1]


def decide(receipts, *, min_sources=2, valid=(2, 8)):
    return adjudicate(
        receipts,
        min_sources=min_sources,
        valid_from=utc(valid[0]),
        valid_to=utc(valid[1]),
    )


class ReleaseTests(unittest.TestCase):
    def test_unanimous_evidence_inside_validity_releases(self) -> None:
        result = decide(
            [receipt(0, 10, "gps"), receipt(4, 6, "ntp")],
            valid=(0, 10),
        )
        self.assertIs(result.verdict, Verdict.RELEASE)
        self.assertTrue(result.fully_valid)
        self.assertFalse(result.fully_invalid)
        self.assertEqual([seg.to_dict() for seg in result.retained], [
            {"start": "2026-11-20T00:04:00Z", "end": "2026-11-20T00:06:00Z"}
        ])
        self.assertEqual(result.valid_segments, result.retained)

    def test_release_keeps_separated_segments_all_inside_validity(self) -> None:
        receipts = [
            receipt(0, 4, "gps"),
            receipt(0, 4, "ntp"),
            receipt(6, 10, "gps"),
            receipt(6, 10, "radio"),
        ]
        result = decide(receipts, valid=(0, 10))
        self.assertIs(result.verdict, Verdict.RELEASE)
        self.assertTrue(result.fully_valid)
        self.assertEqual(len(result.retained), 2)
        self.assertEqual(result.valid_segments, result.retained)

    def test_touching_validity_boundary_is_inclusive(self) -> None:
        result = decide(
            [receipt(10, 10, "gps"), receipt(10, 10, "ntp")],
            valid=(10, 20),
        )
        self.assertIs(result.verdict, Verdict.RELEASE)
        self.assertTrue(covers(result.retained, utc(10)))

    def test_segment_straddling_validity_start_holds(self) -> None:
        """Closed endpoints are valid, but instants before them are not."""

        result = decide(
            [receipt(0, 10, "gps"), receipt(0, 10, "ntp")],
            valid=(10, 20),
        )
        self.assertIs(result.verdict, Verdict.HOLD)
        self.assertEqual([seg.to_dict() for seg in result.valid_segments], [
            {"start": "2026-11-20T00:10:00Z", "end": "2026-11-20T00:10:00Z"}
        ])


class RejectTests(unittest.TestCase):
    def test_all_retained_instants_outside_validity_rejects(self) -> None:
        result = decide(
            [receipt(0, 10, "gps"), receipt(0, 10, "ntp")],
            valid=(20, 30),
        )
        self.assertIs(result.verdict, Verdict.REJECT)
        self.assertTrue(result.fully_invalid)
        self.assertFalse(result.fully_valid)
        self.assertEqual(result.valid_segments, ())

    def test_overlap_just_outside_validity_rejects(self) -> None:
        """Retained 10..12 and validity ending at 9.99 share no instant."""

        result = decide(
            [receipt(0, 12, "gps"), receipt(10, 12, "ntp")],
            valid=(0, 9.99),
        )
        self.assertIs(result.verdict, Verdict.REJECT)

    def test_same_source_copies_are_insufficient_evidence(self) -> None:
        receipts = [receipt(50, 60, "gps") for _ in range(5)]
        result = decide(receipts, min_sources=2, valid=(0, 10))
        self.assertIs(result.verdict, Verdict.HOLD)
        self.assertEqual(result.retained, ())


class HoldTests(unittest.TestCase):
    def test_single_source_is_insufficient_evidence(self) -> None:
        result = decide([receipt(0, 10, "gps")], min_sources=2, valid=(0, 10))
        self.assertIs(result.verdict, Verdict.HOLD)
        self.assertEqual(result.retained, ())
        self.assertIn("insufficient", result.reason)

    def test_mixed_valid_and_invalid_retained_segments_hold(self) -> None:
        receipts = [
            receipt(0, 4, "gps"),
            receipt(0, 4, "ntp"),
            receipt(8, 12, "gps"),
            receipt(8, 12, "radio"),
        ]
        result = decide(receipts, valid=(2, 6))
        self.assertIs(result.verdict, Verdict.HOLD)
        self.assertFalse(result.fully_valid)
        self.assertFalse(result.fully_invalid)
        self.assertEqual(len(result.valid_segments), 1)
        self.assertTrue(covers(result.valid_segments, utc(3)))
        self.assertFalse(covers(result.valid_segments, utc(9)))

    def test_partially_valid_segment_holds_at_leading_cut(self) -> None:
        result = decide([receipt(0, 10, "gps"), receipt(0, 10, "ntp")], valid=(5, 20))
        self.assertIs(result.verdict, Verdict.HOLD)
        self.assertEqual([seg.to_dict() for seg in result.valid_segments], [
            {"start": "2026-11-20T00:05:00Z", "end": "2026-11-20T00:10:00Z"}
        ])

    def test_partially_valid_segment_holds_at_trailing_cut(self) -> None:
        result = decide([receipt(0, 10, "gps"), receipt(0, 10, "ntp")], valid=(0, 5))
        self.assertIs(result.verdict, Verdict.HOLD)
        self.assertEqual([seg.to_dict() for seg in result.valid_segments], [
            {"start": "2026-11-20T00:00:00Z", "end": "2026-11-20T00:05:00Z"}
        ])


class GapSemanticsTests(unittest.TestCase):
    def test_gap_between_trusted_segments_is_not_valid_evidence(self) -> None:
        """Validity sits entirely in the gap between two trusted segments.

        A naive hull would intersect at 3..5 and hold; because gaps carry no
        evidence, every retained instant is invalid and the handshake is
        clearly rejected.
        """

        receipts = [
            receipt(0, 2, "gps"),
            receipt(0, 2, "ntp"),
            receipt(8, 10, "gps"),
            receipt(8, 10, "radio"),
        ]
        result = decide(receipts, valid=(3, 5))
        self.assertIs(result.verdict, Verdict.REJECT)
        self.assertEqual(result.valid_segments, ())
        self.assertTrue(result.fully_invalid)
        self.assertFalse(covers(result.retained, utc(4)))

    def test_gap_does_not_break_segment_level_validity(self) -> None:
        receipts = [
            receipt(0, 2, "gps"),
            receipt(0, 2, "ntp"),
            receipt(8, 10, "gps"),
            receipt(8, 10, "radio"),
        ]
        result = decide(receipts, valid=(0, 10))
        self.assertIs(result.verdict, Verdict.RELEASE)
        self.assertEqual(len(result.retained), 2)
        self.assertFalse(covers(result.valid_segments, utc(5)))


class InterleavedEvidenceTests(unittest.TestCase):
    def test_unsupported_slabs_never_enter_validity(self) -> None:
        """Only 2..8 has two-source support; validity 0..3 cuts it once."""

        receipts = [
            receipt(0, 6, "gps"),
            receipt(2, 8, "ntp"),
            receipt(4, 10, "radio"),
        ]
        result = decide(receipts, min_sources=2, valid=(0, 3))
        self.assertIs(result.verdict, Verdict.HOLD)
        self.assertEqual([seg.to_dict() for seg in result.retained], [
            {"start": "2026-11-20T00:02:00Z", "end": "2026-11-20T00:08:00Z"}
        ])
        self.assertEqual([seg.to_dict() for seg in result.valid_segments], [
            {"start": "2026-11-20T00:02:00Z", "end": "2026-11-20T00:03:00Z"}
        ])

    def test_unauthenticated_receipt_does_not_reach_threshold(self) -> None:
        receipts = [
            receipt(0, 10, "gps"),
            receipt(0, 10, "ntp", authenticated=False),
            receipt(0, 10, "ntp", authenticated=False),
        ]
        result = decide(receipts, min_sources=2, valid=(0, 10))
        self.assertIs(result.verdict, Verdict.HOLD)
        self.assertEqual(result.ignored_unauthenticated, 2)

    def test_interleaved_sources_release_when_entirely_valid(self) -> None:
        receipts = [
            receipt(0, 6, "gps"),
            receipt(2, 8, "ntp"),
            receipt(4, 10, "radio"),
        ]
        result = decide(receipts, min_sources=2, valid=(0, 12))
        self.assertIs(result.verdict, Verdict.RELEASE)
        self.assertEqual(len(result.retained), 1)


class MinSourcesValidationTests(unittest.TestCase):
    def test_min_sources_zero_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            adjudicate([], min_sources=0, valid_from=utc(0), valid_to=utc(1))

    def test_reversed_validity_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            adjudicate([], min_sources=1, valid_from=utc(5), valid_to=utc(1))


class CliTests(unittest.TestCase):
    def _run(self, payload: dict) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, "-m", "handshake_time"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        self.assertEqual(completed.stderr, "")
        return completed.returncode, json.loads(completed.stdout)

    def test_cli_release(self) -> None:
        code, output = self._run(
            {
                "min_sources": 2,
                "valid_from": "2026-11-20T00:00:00Z",
                "valid_to": "2026-11-20T01:00:00Z",
                "receipts": [
                    {"start": "2026-11-20T00:10:00Z", "end": "2026-11-20T00:20:00Z", "source": "gps"},
                    {"start": "2026-11-20T00:15:00Z", "end": "2026-11-20T00:25:00Z", "source": "ntp"},
                ],
            }
        )
        self.assertEqual(code, 0)
        self.assertEqual(output["verdict"], "release")
        self.assertEqual(output["retained"][0]["start"], "2026-11-20T00:15:00Z")
        self.assertEqual(output["retained"][0]["end"], "2026-11-20T00:20:00Z")

    def test_cli_reject(self) -> None:
        code, output = self._run(
            {
                "min_sources": 2,
                "valid_from": "2026-11-20T02:00:00Z",
                "valid_to": "2026-11-20T03:00:00Z",
                "receipts": [
                    {"start": "2026-11-20T00:10:00Z", "end": "2026-11-20T00:20:00Z", "source": "gps"},
                    {"start": "2026-11-20T00:10:00Z", "end": "2026-11-20T00:20:00Z", "source": "ntp"},
                ],
            }
        )
        self.assertEqual(code, 2)
        self.assertEqual(output["verdict"], "reject")

    def test_cli_hold_on_insufficient_evidence(self) -> None:
        code, output = self._run(
            {
                "min_sources": 2,
                "valid_from": "2026-11-20T00:00:00Z",
                "valid_to": "2026-11-20T01:00:00Z",
                "receipts": [
                    {"start": "2026-11-20T00:10:00Z", "end": "2026-11-20T00:20:00Z", "source": "gps"},
                ],
            }
        )
        self.assertEqual(code, 3)
        self.assertEqual(output["verdict"], "hold")

    def test_cli_reports_bad_input(self) -> None:
        code, output = self._run({"min_sources": 2, "valid_from": "bad", "valid_to": "bad"})
        self.assertEqual(code, 1)
        self.assertIn("error", output)


if __name__ == "__main__":
    unittest.main()
