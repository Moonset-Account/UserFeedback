"""Extraction reports: available, missing and conflicting sub-ranges with provenance."""

from __future__ import annotations

import json
import unittest

from _fixtures import DEVICE_A, DEVICE_B, SEG, image, library, not_imaged, read, unreadable

from forensic_segments import SelectionError

# Canonical two-pass scenario.  Segment 0 was read everywhere.  Segment 1 was
# unreadable in the first pass (fill preserved its position) but recovered in
# the retry.  Segment 2 was never imaged by the first pass and disagrees in the
# retry versus a third pass.  Segment 3 only exists in the third pass.
DIGEST = {index: f"sha256:seg{index}" for index in range(4)}
THIRD_DIGEST = "sha256:seg2-other"


def canonical_library():
    return library(
        image(
            "pass1.img",
            [
                read(0, DIGEST[0]),
                unreadable(1, 0x00),
                not_imaged(2),
            ],
            capture="CAP-1",
            device=DEVICE_A,
        ),
        image(
            "pass2.img",
            [
                read(0, DIGEST[0]),
                read(1, DIGEST[1]),
                read(2, DIGEST[2]),
            ],
            capture="CAP-2",
            device=DEVICE_B,
        ),
        image(
            "pass3.img",
            [
                read(0, DIGEST[0]),
                read(1, DIGEST[1]),
                read(2, THIRD_DIGEST),
                read(3, DIGEST[3]),
            ],
            capture="CAP-3",
            device=DEVICE_A,
        ),
    )


class PartitionTests(unittest.TestCase):
    def test_full_range_partitions_into_available_conflict_missing(self):
        report = canonical_library().extract(0, SEG * 4)
        self.assertEqual(report.available_ranges, ((0, SEG * 2), (SEG * 3, SEG * 4)))
        self.assertEqual(report.conflict_ranges, ((SEG * 2, SEG * 3),))
        self.assertEqual(report.missing_ranges, ())
        self.assertEqual(report.available_length, SEG * 3)
        self.assertEqual(report.conflict_length, SEG)
        self.assertEqual(report.missing_length, 0)
        self.assertEqual(report.covered_length, SEG * 4)

    def test_report_is_not_complete_when_conflict_present(self):
        report = canonical_library().extract(0, SEG * 4)
        self.assertFalse(report.is_complete)
        self.assertAlmostEqual(report.coverage, 1.0)

    def test_complete_when_only_unanimous_reads(self):
        report = canonical_library().extract(0, SEG * 2)
        self.assertTrue(report.is_complete)
        self.assertEqual(report.coverage, 1.0)

    def test_missing_range_when_no_image_reads_segment(self):
        lib = library(
            image("a.img", [unreadable(0, 0xAA), read(1, "d1")], capture="CAP1"),
            image("b.img", [unreadable(0, 0x00), read(1, "d1")], capture="CAP2"),
        )
        report = lib.extract(0, SEG * 2)
        self.assertEqual(report.missing_ranges, ((0, SEG),))
        self.assertFalse(report.is_complete)
        self.assertEqual(report.coverage, 0.5)

    def test_missing_range_is_a_gap_not_a_zero_filled_read(self):
        lib = library(image("a.img", [unreadable(0, 0x00)], capture="CAP1"))
        report = lib.extract(0, SEG)
        self.assertEqual(report.segments, ())
        self.assertEqual(report.selected_length, 0)


class PartialRangeTests(unittest.TestCase):
    def test_range_inside_one_segment(self):
        report = canonical_library().extract(100, 200)
        self.assertEqual(len(report.segments), 1)
        seg = report.segments[0]
        self.assertEqual((seg.start, seg.end), (100, 200))
        self.assertEqual(seg.segment_index, 0)
        self.assertEqual(seg.offset_in_segment, 100)
        self.assertEqual(seg.file_offset, 100)
        self.assertEqual(seg.file_offset_end, 200)

    def test_range_spanning_three_segments_offsets_are_exact(self):
        report = canonical_library().extract(400, SEG * 2 + 100)
        self.assertEqual(
            [(s.start, s.end, s.file_offset) for s in report.segments],
            [
                (400, SEG, 400),
                (SEG, SEG * 2, SEG),
                (SEG * 2, SEG * 2 + 100, SEG * 2),
            ],
        )

    def test_clipped_conflict_range_respects_request(self):
        report = canonical_library().extract(SEG * 2 + 10, SEG * 2 + 30)
        self.assertEqual(report.conflict_ranges, ((SEG * 2 + 10, SEG * 2 + 30),))
        self.assertEqual(report.available_ranges, ())
        self.assertEqual(report.segments[0].offset_in_segment, 10)

    def test_empty_range_is_trivially_complete(self):
        report = canonical_library().extract(SEG, SEG)
        self.assertEqual(report.length, 0)
        self.assertEqual(report.segments, ())
        self.assertTrue(report.is_complete)
        self.assertEqual(report.coverage, 1.0)

    def test_range_beyond_all_images_is_missing(self):
        report = canonical_library().extract(SEG * 4, SEG * 5)
        self.assertEqual(report.missing_ranges, ((SEG * 4, SEG * 5),))
        self.assertTrue(report.is_complete is False)


class ProvenanceTests(unittest.TestCase):
    def test_selected_image_and_offset_are_recorded_per_sub_range(self):
        report = canonical_library().extract(0, SEG * 4)
        by_segment = {seg.segment_index: seg for seg in report.segments}

        self.assertEqual(by_segment[0].image_id, "pass1.img")
        self.assertEqual(by_segment[0].capture_id, "CAP-1")
        self.assertEqual(by_segment[0].device_identity, DEVICE_A)

        # pass1 could not read segment 1, so the retry supplies it.
        self.assertEqual(by_segment[1].image_id, "pass2.img")
        self.assertEqual(by_segment[1].capture_id, "CAP-2")

        # Segment 2 is contested; registration order picks pass2.
        self.assertIs(by_segment[2].conflicted, True)
        self.assertEqual(by_segment[2].image_id, "pass2.img")
        self.assertEqual(by_segment[2].digest, DIGEST[2])
        self.assertEqual(by_segment[2].dissenting_captures, ("CAP-3",))

    def test_conflict_keeps_every_candidate_for_review(self):
        report = canonical_library().extract(SEG * 2, SEG * 3)
        seg = report.segments[0]
        statuses = {c.image_id: c.status.value for c in seg.candidates}
        self.assertEqual(
            statuses,
            {"pass1.img": "not_imaged", "pass2.img": "read", "pass3.img": "read"},
        )
        digests = {c.image_id: c.digest for c in seg.candidates if c.eligible}
        self.assertEqual(digests["pass2.img"], DIGEST[2])
        self.assertEqual(digests["pass3.img"], THIRD_DIGEST)

    def test_unavailable_candidates_are_not_counted_as_corroboration(self):
        report = canonical_library().extract(SEG, SEG * 2)
        seg = report.segments[0]
        # CAP-1 only filled this segment; only CAP-2 and CAP-3 confirm content.
        self.assertEqual(seg.corroborating_captures, ("CAP-2", "CAP-3"))
        self.assertEqual(seg.dissenting_captures, ())

    def test_duplicate_copies_do_not_raise_corroboration_count(self):
        lib = library(
            image("orig.img", [read(0, "d0")], capture="CAP1"),
            image("copy.img", [read(0, "d0")], capture="CAP1", derived_from="orig.img"),
            image("other.img", [read(0, "d0")], capture="CAP2"),
        )
        seg = lib.extract(0, SEG).segments[0]
        self.assertEqual(seg.corroborating_captures, ("CAP1", "CAP2"))
        self.assertEqual(len(lib.confirmed_captures(0)), 2)

    def test_only_copy_of_a_fill_capture_stays_missing(self):
        lib = library(
            image("orig.img", [unreadable(0, 0x1A)], capture="CAP1"),
            image(
                "copy.img",
                [unreadable(0, 0x1A)],
                capture="CAP1",
                derived_from="orig.img",
            ),
        )
        report = lib.extract(0, SEG)
        self.assertEqual(report.missing_ranges, ((0, SEG),))
        self.assertEqual(report.fill_notes[0].fill_byte, 0x1A)
        self.assertEqual(len(report.fill_notes), 2)  # both fill-bearing files

    def test_fill_notes_are_clipped_to_request(self):
        lib = library(image("a.img", [unreadable(0, 0x00)], capture="CAP1"))
        report = lib.extract(10, 40)
        self.assertEqual((report.fill_notes[0].start, report.fill_notes[0].end), (10, 40))

    def test_recovered_fill_is_still_recorded(self):
        lib = library(
            image("a.img", [unreadable(0, 0x00)], capture="CAP1"),
            image("b.img", [read(0, "d0")], capture="CAP2"),
        )
        report = lib.extract(0, SEG)
        self.assertEqual(report.missing_ranges, ())
        self.assertEqual(len(report.fill_notes), 1)
        note = report.fill_notes[0]
        self.assertIs(note.covered, True)
        self.assertEqual(note.capture_id, "CAP1")
        # The fill itself is never offered as evidence.
        self.assertEqual(report.segments[0].image_id, "b.img")

    def test_capture_contradicting_itself_is_warned(self):
        # Two non-derived files claim the same original capture but differ.
        lib = library(
            image("a1.img", [read(0, "d0")], capture="CAP1"),
            image("a2.img", [read(0, "d0-tampered")], capture="CAP1"),
        )
        report = lib.extract(0, SEG)
        self.assertTrue(
            any("conflicting digests" in w and "CAP1" in w for w in report.warnings)
        )
        self.assertEqual(report.segments[0].image_id, "a1.img")
        # Self-contradiction is contested even though it is one capture.
        self.assertEqual(report.conflict_ranges, ((0, SEG),))
        self.assertEqual(report.segments[0].corroborating_captures, ("CAP1",))
        self.assertEqual(report.segments[0].dissenting_captures, ("CAP1",))

    def test_identical_copies_of_one_capture_do_not_conflict(self):
        lib = library(
            image("orig.img", [read(0, "d0")], capture="CAP1"),
            image("copy.img", [read(0, "d0")], capture="CAP1", derived_from="orig.img"),
        )
        report = lib.extract(0, SEG)
        self.assertEqual(report.available_ranges, ((0, SEG),))
        self.assertEqual(report.conflict_ranges, ())
        self.assertTrue(report.is_complete)


class PartitionInvariantTests(unittest.TestCase):
    """The three reported categories must exactly partition the request."""

    def _check_partition(self, report):
        intervals = []
        for start, end in report.available_ranges:
            intervals.append((start, end, "available"))
        for start, end in report.conflict_ranges:
            intervals.append((start, end, "conflict"))
        for start, end in report.missing_ranges:
            intervals.append((start, end, "missing"))
        intervals.sort()
        cursor = report.requested_start
        for start, end, _kind in intervals:
            self.assertEqual(start, cursor, f"gap or overlap at {cursor}")
            self.assertLess(start, end)
            cursor = end
        self.assertEqual(cursor, report.requested_end)

    def test_every_range_length_partitions(self):
        lib = canonical_library()
        total = SEG * 4
        for start in range(0, total, 97):
            for end in range(start, total + 1, 131):
                with self.subTest(start=start, end=end):
                    self._check_partition(lib.extract(start, end))

    def test_interleaved_scenario_partitions(self):
        lib = library(
            image(
                "p1.img",
                [read(0, "a"), unreadable(1, 0x00), read(2, "c"), unreadable(3, 0xFF)],
                capture="C1",
            ),
            image(
                "p2.img",
                [unreadable(0, 0x00), read(1, "b"), not_imaged(2), read(3, "d")],
                capture="C2",
            ),
            image(
                "p3.img",
                [read(0, "a"), read(1, "b"), read(2, "c-prime"), read(3, "d")],
                capture="C3",
            ),
        )
        report = lib.extract(0, SEG * 4)
        self._check_partition(report)
        self.assertEqual(report.missing_ranges, ())
        self.assertEqual(report.conflict_ranges, ((SEG * 2, SEG * 3),))
        by_index = {s.segment_index: s for s in report.segments}
        self.assertEqual(by_index[0].image_id, "p1.img")
        self.assertEqual(by_index[1].image_id, "p2.img")
        self.assertEqual(by_index[2].image_id, "p1.img")  # unanimous until C3
        self.assertTrue(by_index[2].conflicted)
        self.assertEqual(by_index[3].image_id, "p2.img")  # p1 only filled it

    def test_partition_holds_when_all_images_are_fills(self):
        lib = library(
            image("a.img", [unreadable(0, 0x00), unreadable(1, 0x01)], capture="C1"),
            image("b.img", [not_imaged(0)], capture="C2"),
        )
        report = lib.extract(0, SEG * 2)
        self._check_partition(report)
        self.assertEqual(report.missing_ranges, ((0, SEG * 2),))
        self.assertEqual(report.selected_length, 0)
        self.assertEqual(report.coverage, 0.0)


class PreferenceTests(unittest.TestCase):
    def test_preferred_image_wins_when_it_has_evidence(self):
        report = canonical_library().extract(0, SEG * 2, preferred=["pass3.img"])
        by_segment = {seg.segment_index: seg for seg in report.segments}
        self.assertEqual(by_segment[0].image_id, "pass3.img")
        self.assertEqual(by_segment[1].image_id, "pass3.img")

    def test_preferred_image_without_evidence_falls_back(self):
        report = canonical_library().extract(SEG, SEG * 2, preferred=["pass1.img"])
        seg = report.segments[0]
        self.assertEqual(seg.segment_index, 1)
        # pass1's segment 1 is a fill and must not be chosen; pass2 falls back in.
        self.assertEqual(seg.image_id, "pass2.img")

    def test_preferred_copy_is_ignored_and_warned(self):
        lib = library(
            image("orig.img", [read(0, "d0")], capture="CAP1"),
            image("copy.img", [read(0, "d0")], capture="CAP1", derived_from="orig.img"),
        )
        report = lib.extract(0, SEG, preferred=["orig.img", "copy.img"])
        self.assertEqual(report.segments[0].image_id, "orig.img")
        self.assertTrue(any("count as one source" in w for w in report.warnings))

    def test_preference_accepts_unknown_image(self):
        lib = canonical_library()
        from forensic_segments import UnknownSourceError

        with self.assertRaises(UnknownSourceError):
            lib.extract(0, SEG, preferred=["ghost.img"])

    def test_preferred_string_instead_of_sequence_rejected(self):
        lib = canonical_library()
        with self.assertRaises(SelectionError):
            lib.extract(0, SEG, preferred="pass1.img")

    def test_repeated_preference_warns(self):
        report = canonical_library().extract(0, SEG, preferred=["pass2.img", "pass2.img"])
        self.assertEqual(report.segments[0].image_id, "pass2.img")
        self.assertTrue(any("more than once" in w for w in report.warnings))

    def test_preference_can_name_derived_copy(self):
        lib = library(
            image("orig.img", [read(0, "d0")], capture="CAP1"),
            image("copy.img", [read(0, "d0")], capture="CAP1", derived_from="orig.img"),
            image("other.img", [read(0, "d0")], capture="CAP2"),
        )
        # Preferring the copy selects the copy file itself for review purposes.
        report = lib.extract(0, SEG, preferred=["copy.img"])
        self.assertEqual(report.segments[0].image_id, "copy.img")
        self.assertEqual(report.segments[0].corroborating_captures, ("CAP1", "CAP2"))


class RangeValidationTests(unittest.TestCase):
    def test_inverted_range_rejected(self):
        with self.assertRaises(SelectionError):
            canonical_library().extract(10, 5)

    def test_negative_values_rejected(self):
        lib = canonical_library()
        with self.assertRaises(SelectionError):
            lib.extract(-1, 10)
        with self.assertRaises(SelectionError):
            lib.extract(0, -5)

    def test_non_integer_rejected(self):
        lib = canonical_library()
        with self.assertRaises(SelectionError):
            lib.extract(0, "10")  # type: ignore[arg-type]
        with self.assertRaises(SelectionError):
            lib.extract(True, 10)  # type: ignore[arg-type]

    def test_empty_library_rejects_non_empty_range(self):
        from forensic_segments import ImageLibrary

        with self.assertRaises(SelectionError):
            ImageLibrary().extract(0, 10)


class ReportSerialisationTests(unittest.TestCase):
    def test_to_dict_round_trips_through_json(self):
        report = canonical_library().extract(0, SEG * 4)
        payload = json.loads(report.to_json())
        self.assertEqual(payload["requested"], {"start": 0, "end": SEG * 4, "length": SEG * 4})
        self.assertEqual(payload["summary"]["conflict_length"], SEG)
        self.assertFalse(payload["summary"]["is_complete"])
        self.assertEqual(payload["conflict_ranges"], [[SEG * 2, SEG * 3]])
        self.assertEqual(len(payload["segments"]), 4)
        self.assertEqual(payload["segments"][2]["image_id"], "pass2.img")
        self.assertIn("candidates", payload["segments"][2])

    def test_render_lists_available_missing_and_conflict(self):
        lib = library(
            image("a.img", [unreadable(0, 0x00), read(1, "d1")], capture="CAP1"),
            image("b.img", [unreadable(0, 0xFF), read(1, "d1")], capture="CAP2"),
        )
        text = lib.extract(0, SEG * 2).render()
        self.assertIn("missing", text)
        self.assertIn("available", text)
        self.assertIn("file_offset=512", text)
        self.assertIn("0x00", text)

    def test_render_shows_conflict_dissent(self):
        text = canonical_library().extract(SEG * 2, SEG * 3).render()
        self.assertIn("conflict", text)
        self.assertIn("pass3.img(CAP-3)=" + THIRD_DIGEST, text)

    def test_covering_helper(self):
        report = canonical_library().extract(400, SEG * 2 + 100)
        self.assertEqual(report.covering(400).segment_index, 0)
        self.assertEqual(report.covering(SEG).segment_index, 1)
        self.assertIsNone(report.covering(SEG * 2 + 100))

    def test_segments_for_image_helper(self):
        report = canonical_library().extract(0, SEG * 4)
        self.assertEqual(
            [s.segment_index for s in report.segments_for_image("pass3.img")], [3]
        )

    def test_select_segment_helper(self):
        seg = canonical_library().select_segment(1, preferred=["pass2.img"])
        self.assertEqual(seg.image_id, "pass2.img")
        self.assertEqual((seg.start, seg.end), (SEG, SEG * 2))

    def test_select_segment_without_evidence_raises(self):
        lib = library(image("a.img", [unreadable(0, 0x00)], capture="CAP1"))
        with self.assertRaises(SelectionError):
            lib.select_segment(0)


if __name__ == "__main__":
    unittest.main()
