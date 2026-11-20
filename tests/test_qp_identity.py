"""Acceptance tests: quoted-printable and identity (7/8bit) encodings."""

from __future__ import annotations

import unittest

from mime_locator import InvalidSelectionError, MimeArchive

from .fixtures import (
    assert_ranges_within_body,
    decode_located_bytes,
    identity_message,
    qp_gap_message,
    simple_multipart_message,
)


class QuotedPrintableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw, self.payload, self.qp_body = qp_gap_message()
        self.archive = MimeArchive(self.raw)
        self.leaf = self.archive.leaves()[0]

    def test_decoded_length_and_soft_break_exclusion(self) -> None:
        # Acceptable result: soft break "=\r\n" adds no decoded byte.
        self.assertEqual(self.leaf.encoding, "quoted-printable")
        self.assertEqual(self.leaf.decoded_length, len(self.payload))
        location = self.archive.locate(self.leaf.leaf_index, 0, len(self.payload))
        raw = location.extract(self.archive.source)
        self.assertNotIn(b"=\r\n", raw)  # soft break not evidence
        self.assertEqual(
            decode_located_bytes(self.archive, location), self.payload
        )
        assert_ranges_within_body(self, self.archive, self.leaf, location)

    def test_hex_escape_maps_to_three_raw_bytes_one_decoded(self) -> None:
        # Decoded layout: b e f o r e C3 A9 a f t e r
        # The first hex escape '=C3' produces decoded index 6 and occupies
        # three raw bytes starting at body offset 6.
        location = self.archive.locate(self.leaf.leaf_index, 6, 7)
        self.assertEqual(
            location.raw_ranges,
            ((self.leaf.raw_body_start + 6, self.leaf.raw_body_start + 9),),
        )
        self.assertEqual(location.extract(self.archive.source), b"=C3")

    def test_selection_crossing_soft_break_is_interleaved_ranges(self) -> None:
        # QP body: before=C3=A9or=<CRLF>after
        # Decoded:  b e f o r e C3 A9 o r a f t e r  (indexes 0..14)
        # The soft break sits between decoded index 9 ('r') and 10 ('a').
        # Selecting the full body through the gap splices the transport
        # CRLF out while preserving every evidence byte on both sides.
        location = self.archive.locate(
            self.leaf.leaf_index, 0, self.leaf.decoded_length
        )
        joined = location.extract(self.archive.source)
        self.assertNotIn(b"=\r\n", joined)  # soft break not evidence
        self.assertNotIn(b"\r", joined)
        self.assertEqual(
            decode_located_bytes(self.archive, location), self.payload
        )
        # The hex escape just before the gap remains part of the evidence.
        self.assertIn(b"=A9", joined)

    def test_hard_line_break_round_trip(self) -> None:
        raw, text_payload, _ = simple_multipart_message()
        archive = MimeArchive(raw)
        leaf = archive.leaves()[0]
        self.assertEqual(leaf.encoding, "quoted-printable")
        location = archive.locate(leaf.leaf_index, 0, leaf.decoded_length)
        # The final CRLF doubles as the boundary-leading framing break and is
        # therefore excluded; every interior hard line break round-trips.
        self.assertEqual(
            decode_located_bytes(archive, location), text_payload[:-2]
        )
        interior = archive.locate(leaf.leaf_index, 0, leaf.decoded_length - 1)
        self.assertIn(b"\n", decode_located_bytes(archive, interior))

    def test_malformed_qp_is_rejected_as_corrupted(self) -> None:
        from .fixtures import message, multipart, part

        body = multipart(
            "B-Q",
            [
                part(
                    [
                        "Content-Type: text/plain",
                        "Content-Transfer-Encoding: quoted-printable",
                    ],
                    b"bad =ZZ escape\r\n",
                )
            ],
        )
        raw = message(
            ["MIME-Version: 1.0", 'Content-Type: multipart/mixed; boundary="B-Q"'],
            body,
        )
        archive = MimeArchive(raw)
        leaf = archive.leaves()[0]
        self.assertTrue(leaf.is_corrupted)
        with self.assertRaises(InvalidSelectionError):
            archive.locate(leaf.leaf_index, 0, 1)

    def test_out_of_range_rejected(self) -> None:
        with self.assertRaises(InvalidSelectionError):
            self.archive.locate(
                self.leaf.leaf_index, 0, self.leaf.decoded_length + 1
            )


class IdentityEncodingTests(unittest.TestCase):
    def test_8bit_body_maps_one_to_one(self) -> None:
        raw, payload = identity_message()
        archive = MimeArchive(raw)
        leaf = archive.leaves()[0]
        self.assertEqual(leaf.encoding, "8bit")
        self.assertEqual(leaf.decoded_length, len(payload))
        location = archive.locate(leaf.leaf_index, 2, 9)
        self.assertEqual(location.raw_ranges, ((leaf.raw_body_start + 2,
                                                 leaf.raw_body_start + 9),))
        self.assertEqual(location.extract(archive.source), payload[2:9])
        assert_ranges_within_body(self, archive, leaf, location)

    def test_full_identity_selection(self) -> None:
        raw, payload = identity_message()
        archive = MimeArchive(raw)
        leaf = archive.leaves()[0]
        location = archive.locate(leaf.leaf_index, 0, len(payload))
        self.assertEqual(location.extract(archive.source), payload)


if __name__ == "__main__":
    unittest.main()
