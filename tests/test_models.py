"""Validation and immutability of the metadata value objects."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from _fixtures import SEG, image, not_imaged, read, unreadable

from forensic_segments import Image, Segment, SegmentStatus, ValidationError


class SegmentValidationTests(unittest.TestCase):
    def test_read_requires_digest(self):
        with self.assertRaises(ValidationError):
            Segment(0, SegmentStatus.READ)

    def test_read_rejects_blank_digest(self):
        with self.assertRaises(ValidationError):
            Segment(0, SegmentStatus.READ, digest="   ")

    def test_read_rejects_fill_byte(self):
        with self.assertRaises(ValidationError):
            Segment(0, SegmentStatus.READ, digest="abc", fill_byte=0)

    def test_unreadable_requires_fill_byte(self):
        with self.assertRaises(ValidationError):
            Segment(0, SegmentStatus.UNREADABLE)

    def test_unreadable_fill_out_of_range(self):
        with self.assertRaises(ValidationError):
            Segment(0, SegmentStatus.UNREADABLE, fill_byte=256)
        with self.assertRaises(ValidationError):
            Segment(0, SegmentStatus.UNREADABLE, fill_byte=-1)

    def test_unreadable_rejects_digest(self):
        with self.assertRaises(ValidationError):
            Segment(0, SegmentStatus.UNREADABLE, fill_byte=0, digest="abc")

    def test_not_imaged_rejects_content(self):
        with self.assertRaises(ValidationError):
            Segment(0, SegmentStatus.NOT_IMAGED, fill_byte=0)
        with self.assertRaises(ValidationError):
            Segment(0, SegmentStatus.NOT_IMAGED, digest="abc")

    def test_negative_index_rejected(self):
        with self.assertRaises(ValidationError):
            Segment(-1, SegmentStatus.NOT_IMAGED)

    def test_bool_index_rejected(self):
        with self.assertRaises(ValidationError):
            Segment(True, SegmentStatus.NOT_IMAGED)

    def test_string_status_is_coerced(self):
        seg = Segment(0, "read", digest="abc")
        self.assertIs(seg.status, SegmentStatus.READ)

    def test_unknown_status_rejected(self):
        with self.assertRaises(ValidationError):
            Segment(0, "partially-read", digest="abc")

    def test_is_evidence_only_for_read(self):
        self.assertTrue(read(0, "d").is_evidence)
        self.assertFalse(unreadable(0).is_evidence)
        self.assertFalse(not_imaged(0).is_evidence)

    def test_segment_is_frozen(self):
        seg = read(0, "d")
        with self.assertRaises(FrozenInstanceError):
            seg.digest = "other"  # type: ignore[misc]


class ImageValidationTests(unittest.TestCase):
    def test_empty_ids_rejected(self):
        for kwargs in (
            {"image_id": "", "capture_id": "c", "device_identity": "d"},
            {"image_id": "i", "capture_id": " ", "device_identity": "d"},
            {"image_id": "i", "capture_id": "c", "device_identity": ""},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValidationError):
                    Image(segment_size=SEG, segments=(read(0, "a"),), **kwargs)

    def test_segment_size_must_be_positive_int(self):
        with self.assertRaises(ValidationError):
            Image("i", "d", "c", 0, ())
        with self.assertRaises(ValidationError):
            Image("i", "d", "c", True, ())

    def test_indexes_must_be_contiguous(self):
        with self.assertRaises(ValidationError):
            image("i", [read(0, "a"), read(2, "b")])
        with self.assertRaises(ValidationError):
            image("i", [read(1, "a")])

    def test_segment_size_mismatch(self):
        library_image = image("i", [read(0, "a")], segment_size=512)
        other = image("j", [read(0, "a")], segment_size=1024)
        self.assertNotEqual(library_image.segment_size, other.segment_size)

    def test_derived_from_self_rejected(self):
        with self.assertRaises(ValidationError):
            Image("i", "d", "c", SEG, (read(0, "a"),), derived_from="i")

    def test_byte_length_and_lookup(self):
        img = image("i", [read(0, "a"), read(1, "b")])
        self.assertEqual(img.segment_count, 2)
        self.assertEqual(img.byte_length, SEG * 2)
        self.assertEqual(img.segment_for_offset(0).index, 0)
        self.assertEqual(img.segment_for_offset(SEG).index, 1)
        self.assertIsNone(img.segment_for_offset(SEG * 2))
        self.assertIsNone(img.segment_for_offset(-1))
        self.assertIsNone(img.segment_at(5))

    def test_image_equality_is_value_based(self):
        self.assertEqual(image("i", [read(0, "a")]), image("i", [read(0, "a")]))
        self.assertNotEqual(image("i", [read(0, "a")]), image("i", [read(0, "b")]))


if __name__ == "__main__":
    unittest.main()
