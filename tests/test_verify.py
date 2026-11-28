"""Tests for the playback verifier itself (it must catch tampering)."""

import unittest

from fontsubset import (
    SubsetVerificationError,
    subset_font,
    verify_subset,
)

from fixtures import build_latin_font


class TestVerifySubset(unittest.TestCase):
    def setUp(self):
        self.font = build_latin_font()
        self.texts = ["fi", "fif", "ab"]

    def test_good_subset_passes(self):
        result = subset_font(self.font, self.texts)
        report = verify_subset(self.font, result.font, self.texts)
        self.assertTrue(report.ok)
        self.assertIn("all texts verified", str(report))

    def test_dropped_rule_is_detected(self):
        result = subset_font(self.font, self.texts)
        tampered = result.font
        tampered.rules = [r for r in tampered.rules
                          if len(r.output) != 1]  # drop the ligature
        report = verify_subset(self.font, tampered, self.texts)
        self.assertFalse(report.ok)
        failed = [r for r in report.records if not r.ok]
        self.assertEqual({r.text for r in failed}, {"fi", "fif"})
        self.assertIn("glyph count differs", failed[0].detail)
        self.assertIn("FAILED", str(report))

    def test_advance_tampering_is_detected_via_positions(self):
        result = subset_font(self.font, ["fif"])
        tampered = result.font
        fi_gid = tampered.name_to_gid["fi"]
        tampered.glyphs[fi_gid].advance += 1  # shifts the trailing "f"
        report = verify_subset(self.font, tampered, ["fif"])
        self.assertFalse(report.ok)
        self.assertIn("differs", report.records[0].detail)

    def test_verification_error_carries_report(self):
        result = subset_font(self.font, self.texts)
        result.font.rules = []
        report = verify_subset(self.font, result.font, self.texts)
        err = SubsetVerificationError(report)
        self.assertIs(err.report, report)
        self.assertIn("'fi'", str(err))


if __name__ == "__main__":
    unittest.main()
