"""Eligibility: fill bytes never qualify; copies are one source."""

from __future__ import annotations

import unittest

from _fixtures import SEG, image, library, not_imaged, read, unreadable

from forensic_segments import SelectionError, SegmentStatus


class EligibilityTests(unittest.TestCase):
    def test_fill_segment_is_not_referenceable(self):
        lib = library(image("a.img", [unreadable(0, 0x00)], capture="CAP1"))
        cands = lib.segment_candidates(0)
        self.assertEqual(len(cands), 1)
        self.assertFalse(cands[0].eligible)
        self.assertIs(cands[0].status, SegmentStatus.UNREADABLE)
        self.assertEqual(cands[0].fill_byte, 0x00)
        self.assertEqual(lib.confirmed_captures(0), ())

    def test_fill_keeps_position_so_later_segments_align(self):
        # Segment 0 unreadable, 1 readable.  Index 1 must still be at byte 512.
        lib = library(
            image("a.img", [unreadable(0, 0xFF), read(1, "d1")], capture="CAP1")
        )
        report = lib.extract(0, SEG * 2)
        self.assertEqual(report.missing_ranges, ((0, SEG),))
        self.assertEqual(len(report.segments), 1)
        seg = report.segments[0]
        self.assertEqual(seg.start, SEG)
        self.assertEqual(seg.end, SEG * 2)
        self.assertEqual(seg.file_offset, SEG)
        self.assertEqual(seg.segment_index, 1)

    def test_not_imaged_is_not_eligible(self):
        lib = library(image("a.img", [not_imaged(0)], capture="CAP1"))
        self.assertFalse(lib.segment_candidates(0)[0].eligible)

    def test_short_image_reports_not_imaged_for_later_segments(self):
        lib = library(
            image("a.img", [read(0, "d0")], capture="CAP1"),
            image("b.img", [read(0, "d0"), read(1, "d1")], capture="CAP2"),
        )
        cands = lib.segment_candidates(1)
        by_id = {c.image_id: c for c in cands}
        self.assertIs(by_id["a.img"].status, SegmentStatus.NOT_IMAGED)
        self.assertFalse(by_id["a.img"].eligible)
        self.assertTrue(by_id["b.img"].eligible)

    def test_copy_of_same_capture_counts_once(self):
        lib = library(
            image("orig.img", [read(0, "d0")], capture="CAP1"),
            image("copy.img", [read(0, "d0")], capture="CAP1", derived_from="orig.img"),
        )
        self.assertEqual(lib.confirmed_captures(0), ("CAP1",))
        report = lib.extract(0, SEG)
        self.assertEqual(len(report.segments), 1)
        self.assertEqual(report.segments[0].corroborating_captures, ("CAP1",))

    def test_two_real_captures_both_count(self):
        lib = library(
            image("a.img", [read(0, "d0")], capture="CAP1"),
            image("b.img", [read(0, "d0")], capture="CAP2"),
        )
        self.assertEqual(lib.confirmed_captures(0), ("CAP1", "CAP2"))
        report = lib.extract(0, SEG)
        self.assertEqual(report.segments[0].corroborating_captures, ("CAP1", "CAP2"))

    def test_negative_segment_index_rejected(self):
        lib = library(image("a.img", [read(0, "d0")]))
        with self.assertRaises(SelectionError):
            lib.segment_candidates(-1)


if __name__ == "__main__":
    unittest.main()
