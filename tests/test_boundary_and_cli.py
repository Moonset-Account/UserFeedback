"""Boundary, interleaving and malformed-input acceptance tests."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from mime_locator import InvalidSelectionError, MimeArchive, MimeParseError
from mime_locator.cli import main as cli_main

from .fixtures import (
    canonical_base64,
    message,
    multipart,
    part,
    simple_multipart_message,
)


class BoundaryInterleavingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw, _, self.bin_payload = simple_multipart_message()
        self.archive = MimeArchive(self.raw)
        self.leaf = self.archive.leaves()[1]

    def test_selection_ending_exactly_on_group_boundary(self) -> None:
        # 12 decoded bytes = exactly four 3-byte groups.
        location = self.archive.locate(self.leaf.leaf_index, 0, 12)
        self.assertEqual((location.covered_start, location.covered_end), (0, 12))
        self.assertEqual(len(location.extract(self.archive.source)), 16)

    def test_selection_starting_exactly_on_group_boundary(self) -> None:
        # 12..16 intersects the [12,15) group fully and the [15,18) group
        # partially, so the end expands to the second group's edge.
        location = self.archive.locate(self.leaf.leaf_index, 12, 16)
        self.assertEqual(
            (location.covered_start, location.covered_end), (12, 18)
        )

    def test_partial_group_at_both_ends_expands(self) -> None:
        # Start one byte into the [6,9) group, end one byte into the
        # [9,12) group; both whole groups are returned.
        location = self.archive.locate(self.leaf.leaf_index, 7, 10)
        self.assertEqual((location.covered_start, location.covered_end), (6, 12))
        import base64

        self.assertEqual(
            base64.b64decode(location.extract(self.archive.source)),
            self.bin_payload[6:12],
        )

    def test_full_range_then_empty_range_are_consistent(self) -> None:
        full = self.archive.locate(
            self.leaf.leaf_index, 0, self.leaf.decoded_length
        )
        self.assertTrue(full.raw_ranges)
        empty = self.archive.locate(
            self.leaf.leaf_index, self.leaf.decoded_length,
            self.leaf.decoded_length,
        )
        self.assertEqual(empty.raw_ranges, ())
        # Empty selection at EOF is a legal boundary cursor.
        self.assertEqual(empty.covered_start, self.leaf.decoded_length)

    def test_non_integer_offsets_rejected(self) -> None:
        with self.assertRaises(InvalidSelectionError):
            self.archive.locate(self.leaf.leaf_index, 1.5, 2)  # type: ignore[arg-type]
        with self.assertRaises(InvalidSelectionError):
            self.archive.locate(self.leaf.leaf_index, True, 2)  # type: ignore[arg-type]

    def test_ranges_always_sorted_merged_and_half_open(self) -> None:
        location = self.archive.locate(
            self.leaf.leaf_index, 0, self.leaf.decoded_length
        )
        ranges = location.raw_ranges
        self.assertEqual(ranges, tuple(sorted(ranges)))
        for left, right in zip(ranges, ranges[1:]):
            self.assertLessEqual(left[1], right[0])
        for start, end in ranges:
            self.assertLess(start, end)


class MalformedInputTests(unittest.TestCase):
    def test_empty_source_rejected(self) -> None:
        with self.assertRaises(MimeParseError):
            MimeArchive(b"")

    def test_missing_header_separator_rejected(self) -> None:
        with self.assertRaises(MimeParseError):
            MimeArchive(b"Content-Type: text/plain\r\nno blank line body")

    def test_multipart_without_boundary_rejected(self) -> None:
        raw = message(
            ["MIME-Version: 1.0", "Content-Type: multipart/mixed"],
            b"--X\r\n",
        )
        with self.assertRaises(MimeParseError):
            MimeArchive(raw)

    def test_unclosed_multipart_rejected(self) -> None:
        raw = message(
            ["MIME-Version: 1.0", 'Content-Type: multipart/mixed; boundary="X"'],
            b"--X\r\nContent-Type: text/plain\r\n\r\nbody\r\n",
        )
        with self.assertRaises(MimeParseError):
            MimeArchive(raw)

    def test_non_bytes_source_rejected(self) -> None:
        with self.assertRaises((TypeError, MimeParseError)):
            MimeArchive("not bytes")  # type: ignore[arg-type]

    def test_base64_invalid_character_part_corrupted(self) -> None:
        body = multipart(
            "B-BAD",
            [
                part(
                    [
                        "Content-Type: application/octet-stream",
                        "Content-Transfer-Encoding: base64",
                    ],
                    b"QU#D\r\n",
                ),
                part(
                    ["Content-Type: text/plain"],
                    b"surviving sibling\r\n",
                ),
            ],
        )
        raw = message(
            ["MIME-Version: 1.0", 'Content-Type: multipart/mixed; boundary="B-BAD"'],
            body,
        )
        archive = MimeArchive(raw)
        leaves = archive.leaves()
        self.assertTrue(leaves[0].is_corrupted)
        self.assertFalse(leaves[1].is_corrupted)
        okay = archive.locate(leaves[1].leaf_index, 0, 9)
        self.assertEqual(okay.extract(archive.source), b"surviving")


class PrecomputeFixtureSanityTests(unittest.TestCase):
    def test_fixture_base64_body_contains_soft_wraps(self) -> None:
        raw, _, _ = simple_multipart_message()
        # The generated source must genuinely contain CRLF inside the b64 part.
        archive = MimeArchive(raw)
        leaf = archive.leaves()[1]
        body = raw[leaf.raw_body_start:leaf.raw_body_end]
        self.assertIn(b"\r\n", body)
        import base64

        compact = b"".join(body.split())
        self.assertEqual(
            canonical_base64(base64.b64decode(compact), 24),
            body if body.endswith(b"\r\n") else body + b"\r\n",
        )


class CliSmokeTests(unittest.TestCase):
    def test_inspect_and_locate(self) -> None:
        raw, _, _ = simple_multipart_message()
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "msg.eml"
            path.write_bytes(raw)
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_main(["inspect", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("multipart: multipart/mixed", out.getvalue())
            self.assertIn("leaf=1", out.getvalue())

            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_main(
                    ["locate", str(path), "--leaf", "1",
                     "--start", "10", "--end", "11"]
                )
            self.assertEqual(code, 0)
            self.assertIn("raw:", out.getvalue())

    def test_locate_bad_range_exit_code(self) -> None:
        import tempfile
        from pathlib import Path

        raw, _, _ = simple_multipart_message()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "msg.eml"
            path.write_bytes(raw)
            err = io.StringIO()
            with redirect_stderr(err):
                code = cli_main(
                    ["locate", str(path), "--leaf", "1",
                     "--start", "0", "--end", "999999"]
                )
            self.assertEqual(code, 2)
            self.assertIn("error:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
