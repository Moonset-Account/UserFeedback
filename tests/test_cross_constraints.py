"""End-to-end cross-constraint acceptance test.

This single scenario exercises every headline requirement together:
explicit multipart boundaries, nested message/rfc822, base64 and
quoted-printable transfer encodings, decoded-byte selection with full-group
attribution, exclusion of soft wraps/boundary lines, duplicate-instance
distinction and corruption isolation of sibling attachments.
"""

from __future__ import annotations

import base64
import unittest

from mime_locator import (
    CorruptedPartError,
    InvalidSelectionError,
    MimeArchive,
)

from .fixtures import CRLF, message, multipart, part


def build_exhibit() -> bytes:
    # Inner forwarded message with two identical base64 attachments and one
    # broken one, plus a quoted-printable note.
    inner_boundary = "INNER-X"
    payload = b"forensic byte selection payload!"  # 33 bytes, one padded group
    encoded = base64.b64encode(payload)
    # Insert a soft wrap in the middle of the base64 stream.
    soft_wrapped = encoded[:16] + CRLF + encoded[16:] + CRLF
    healthy = part(
        [
            "Content-Type: application/octet-stream",
            'Content-Disposition: attachment; filename="dup.bin"',
            "Content-Transfer-Encoding: base64",
        ],
        soft_wrapped,
    )
    corrupted = part(
        [
            "Content-Type: application/octet-stream",
            'Content-Disposition: attachment; filename="broken.bin"',
            "Content-Transfer-Encoding: base64",
        ],
        b"!!!!not base64!!!!\r\n",
    )
    note = part(
        [
            "Content-Type: text/plain",
            "Content-Transfer-Encoding: quoted-printable",
        ],
        # Hard CRLF between the two lines; a soft break splits the second word.
        b"line=20one\r\nline=20t=\r\nwo\r\n",
    )
    inner_body = multipart(inner_boundary, [healthy, healthy, corrupted, note])
    inner = message(
        [
            "MIME-Version: 1.0",
            "From: forwarder@example.invalid",
            f'Content-Type: multipart/mixed; boundary="{inner_boundary}"',
        ],
        inner_body,
    )

    outer_boundary = "OUTER-X"
    cover = part(
        ["Content-Type: text/plain", "Content-Transfer-Encoding: 7bit"],
        b"please review the forwarded exhibit\r\n",
    )
    rfc822 = part(
        ["Content-Type: message/rfc822"],
        inner,
    )
    outer_body = multipart(outer_boundary, [cover, rfc822])
    return message(
        [
            "MIME-Version: 1.0",
            f'Content-Type: multipart/mixed; boundary="{outer_boundary}"',
        ],
        outer_body,
    )


class CrossConstraintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = build_exhibit()
        self.archive = MimeArchive(self.source)

    def test_all_cross_constraints_together(self) -> None:
        leaves = self.archive.leaves()
        # cover(7bit), dup#0(b64), dup#1(b64), broken(b64), note(qp)
        self.assertEqual(len(leaves), 5)

        # ---- nested hierarchy path ----
        dup0, dup1, broken, qp_note = leaves[1], leaves[2], leaves[3], leaves[4]
        expected_prefix = "root > part[1] > rfc822[0] > part["
        self.assertTrue(dup0.path_string().startswith(expected_prefix + "0]"))
        self.assertTrue(dup1.path_string().startswith(expected_prefix + "1]"))
        self.assertTrue(broken.path_string().startswith(expected_prefix + "2]"))
        self.assertTrue(qp_note.path_string().startswith(expected_prefix + "3]"))

        # ---- single byte expands to the whole base64 group ----
        loc = self.archive.locate(dup0.leaf_index, 10, 11)
        self.assertEqual((loc.covered_start, loc.covered_end), (9, 12))
        raw_group = loc.extract(self.source)
        self.assertEqual(len(raw_group), 4)
        self.assertEqual(base64.b64decode(raw_group), b"byt")  # payload[9:12]

        # ---- soft line wrap and boundary lines are never evidence ----
        full = self.archive.locate(dup0.leaf_index, 0, dup0.decoded_length)
        evidence = full.extract(self.source)
        self.assertNotIn(b"\r", evidence)
        self.assertNotIn(b"\n", evidence)
        self.assertNotIn(b"INNER-X", evidence)
        self.assertNotIn(b"OUTER-X", evidence)
        self.assertEqual(base64.b64decode(evidence), b"forensic byte selection payload!")

        # ---- duplicate content stays distinct by attachment instance ----
        self.assertNotEqual(dup0.leaf_index, dup1.leaf_index)
        self.assertNotEqual(
            self.archive.locate(dup0.leaf_index, 0, 3).raw_ranges,
            self.archive.locate(dup1.leaf_index, 0, 3).raw_ranges,
        )

        # ---- corrupted attachment is blocked, siblings stay locatable ----
        self.assertTrue(broken.is_corrupted)
        with self.assertRaises(CorruptedPartError):
            self.archive.locate(broken.leaf_index, 0, 1)
        for healthy_leaf in (leaves[0], dup0, dup1, qp_note):
            self.assertFalse(healthy_leaf.is_corrupted)
            ok = self.archive.locate(
                healthy_leaf.leaf_index, 0,
                min(3, healthy_leaf.decoded_length),
            )
            self.assertTrue(ok.raw_ranges)

        # ---- QP soft break exclusion round-trip ----
        note_loc = self.archive.locate(
            qp_note.leaf_index, 0, qp_note.decoded_length
        )
        import quopri

        self.assertEqual(
            quopri.decodestring(note_loc.extract(self.source)),
            b"line one\r\nline two",
        )

        # ---- rejected: out-of-range and reversed selections ----
        with self.assertRaises(InvalidSelectionError):
            self.archive.locate(dup0.leaf_index, 0, dup0.decoded_length + 1)
        with self.assertRaises(InvalidSelectionError):
            self.archive.locate(dup0.leaf_index, 5, 2)

        # ---- boundary/interleaving: zero-width selection has no ranges ----
        cursor = self.archive.locate(dup0.leaf_index, 12, 12)
        self.assertEqual(cursor.raw_ranges, ())
        self.assertEqual((cursor.covered_start, cursor.covered_end), (12, 12))

        # ---- read-only guarantee ----
        self.assertEqual(self.archive.source, self.source)


if __name__ == "__main__":
    unittest.main()
