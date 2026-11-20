"""Acceptance tests: base64 multipart with soft line wrapping.

Covers the cross-constraint that a single selected decoded byte must be
attributed to its complete four-character base64 group, and that soft line
breaks are never part of the reported raw evidence.
"""

from __future__ import annotations

import unittest

from mime_locator import (
    CorruptedPartError,
    InvalidSelectionError,
    MimeArchive,
)

from .fixtures import (
    assert_ranges_within_body,
    canonical_base64,
    decode_located_bytes,
    simple_multipart_message,
)


class Base64MultipartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw, self.text_payload, self.bin_payload = (
            simple_multipart_message()
        )
        self.archive = MimeArchive(self.raw)
        self.leaves = self.archive.leaves()
        self.bin_leaf = self.leaves[1]

    def test_tree_structure_and_leaf_identity(self) -> None:
        # Acceptable result: the tree exposes two independently addressable
        # leaves with distinct paths.
        self.assertEqual(len(self.leaves), 2)
        self.assertEqual(self.bin_leaf.encoding, "base64")
        self.assertEqual(self.bin_leaf.decoded_length, len(self.bin_payload))
        self.assertEqual(
            self.bin_leaf.path_string(), "root > part[1]"
        )
        self.assertFalse(self.bin_leaf.is_corrupted)

    def test_single_byte_selection_expands_to_full_group(self) -> None:
        # Select decoded byte at index 10 (a single byte inside one group).
        # Groups decode 3 bytes each: index 10 lies in the [9, 12) group.
        location = self.archive.locate(self.bin_leaf.leaf_index, 10, 11)
        self.assertEqual(location.requested_start, 10)
        self.assertEqual(location.requested_end, 11)
        # The whole 4-char group is covered: decoded span is group-aligned.
        self.assertEqual(location.covered_start, 9)
        self.assertEqual(location.covered_end, 12)
        raw = location.extract(self.archive.source)
        self.assertEqual(len(raw), 4)
        # And the group independently re-decodes to the expected three bytes.
        import base64 as b64

        self.assertEqual(b64.b64decode(raw), self.bin_payload[9:12])

    def test_group_aligned_multi_group_selection(self) -> None:
        location = self.archive.locate(self.bin_leaf.leaf_index, 0, 12)
        self.assertEqual((location.covered_start, location.covered_end), (0, 12))
        # Four groups, each four raw characters (soft wraps spliced out).
        raw = location.extract(self.archive.source)
        self.assertEqual(len(raw), 16)
        self.assertEqual(
            decode_located_bytes(self.archive, location),
            self.bin_payload[0:12],
        )

    def test_soft_line_breaks_excluded_from_ranges(self) -> None:
        # Force a selection spanning the 24-char soft-wrap boundary (groups
        # covering decoded bytes 0..17 cross at least one CRLF).
        location = self.archive.locate(self.bin_leaf.leaf_index, 0, 18)
        raw = location.extract(self.archive.source)
        self.assertNotIn(b"\r", raw)
        self.assertNotIn(b"\n", raw)
        self.assertEqual(len(raw), 24)  # six clean 4-char groups
        # Ranges may be split pieces, but their concatenation must still
        # decode identically to the selected groups.
        self.assertEqual(
            decode_located_bytes(self.archive, location),
            self.bin_payload[0:18],
        )
        assert_ranges_within_body(
            self, self.archive, self.bin_leaf, location
        )

    def test_full_body_selection_reconstructs_payload(self) -> None:
        location = self.archive.locate(
            self.bin_leaf.leaf_index, 0, len(self.bin_payload)
        )
        self.assertEqual(
            decode_located_bytes(self.archive, location), self.bin_payload
        )
        assert_ranges_within_body(
            self, self.archive, self.bin_leaf, location
        )

    def test_boundary_line_never_covered(self) -> None:
        location = self.archive.locate(
            self.bin_leaf.leaf_index, 0, len(self.bin_payload)
        )
        covered_blob = b"".join(
            self.archive.source[s:e] for s, e in location.raw_ranges
        )
        self.assertNotIn(b"BOUND-MAIN-01", covered_blob)
        for start, end in location.raw_ranges:
            self.assertGreaterEqual(start, self.bin_leaf.raw_body_start)
            self.assertLessEqual(end, self.bin_leaf.raw_body_end)

    # ---- rejected / boundary scenarios ----

    def test_selection_beyond_decoded_length_rejected(self) -> None:
        with self.assertRaises(InvalidSelectionError):
            self.archive.locate(
                self.bin_leaf.leaf_index,
                len(self.bin_payload),
                len(self.bin_payload) + 1,
            )

    def test_negative_and_reversed_selection_rejected(self) -> None:
        with self.assertRaises(InvalidSelectionError):
            self.archive.locate(self.bin_leaf.leaf_index, -1, 2)
        with self.assertRaises(InvalidSelectionError):
            self.archive.locate(self.bin_leaf.leaf_index, 5, 4)

    def test_zero_width_selection_is_boundary_with_no_ranges(self) -> None:
        location = self.archive.locate(self.bin_leaf.leaf_index, 10, 10)
        self.assertEqual(location.raw_ranges, ())
        self.assertEqual(location.covered_start, 10)
        self.assertEqual(location.covered_end, 10)

    def test_unknown_leaf_index_rejected(self) -> None:
        with self.assertRaises(InvalidSelectionError):
            self.archive.locate(99, 0, 1)

    def test_source_is_not_mutated(self) -> None:
        snapshot = bytes(self.raw)
        _ = MimeArchive(self.raw)
        self.assertEqual(bytes(self.raw), snapshot)

    def test_corrupted_base64_fixture_is_flagged(self) -> None:
        # A damaged group anywhere in the part marks that part corrupted.
        broken = canonical_base64(b"abcdefg")[:-4] + b"X\r\n"
        from .fixtures import multipart, message, part

        body = multipart(
            "B-X",
            [
                part(
                    [
                        "Content-Type: application/octet-stream",
                        "Content-Transfer-Encoding: base64",
                    ],
                    broken,
                )
            ],
        )
        raw = message(
            ["MIME-Version: 1.0", 'Content-Type: multipart/mixed; boundary="B-X"'],
            body,
        )
        archive = MimeArchive(raw)
        leaf = archive.leaves()[0]
        self.assertTrue(leaf.is_corrupted)
        with self.assertRaises(CorruptedPartError):
            archive.locate(leaf.leaf_index, 0, 1)


if __name__ == "__main__":
    unittest.main()
