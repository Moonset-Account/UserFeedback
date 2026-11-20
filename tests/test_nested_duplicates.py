"""Acceptance tests: nested message/rfc822, duplicate and corrupted parts."""

from __future__ import annotations

import unittest

from mime_locator import (
    CorruptedPartError,
    InvalidSelectionError,
    MimeArchive,
)

from .fixtures import (
    decode_located_bytes,
    duplicates_and_corruption_message,
    nested_message,
)


class NestedRfc822Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw, self.inner_payload = nested_message()
        self.archive = MimeArchive(self.raw)

    def test_hierarchy_paths(self) -> None:
        leaves = self.archive.leaves()
        # outer text, then rfc822 -> inner text (base64), inner note
        self.assertEqual(len(leaves), 3)
        self.assertEqual(leaves[0].path_string(), "root > part[0]")
        self.assertEqual(
            leaves[1].path_string(),
            "root > part[1] > rfc822[0] > part[0]",
        )
        self.assertEqual(
            leaves[2].path_string(),
            "root > part[1] > rfc822[0] > part[1]",
        )
        rfc822 = self.archive.root.children[1]
        self.assertEqual(rfc822.kind, "rfc822")
        self.assertEqual(rfc822.children[0].kind, "multipart")

    def test_locate_inside_nested_message(self) -> None:
        inner_leaf = self.archive.leaves()[1]
        self.assertEqual(inner_leaf.encoding, "base64")
        self.assertEqual(inner_leaf.decoded_length, len(self.inner_payload))
        # Single byte expands to its whole base64 group.
        location = self.archive.locate(inner_leaf.leaf_index, 0, 1)
        self.assertEqual(location.covered_start, 0)
        self.assertEqual(location.covered_end, 3)
        self.assertEqual(
            decode_located_bytes(self.archive, location),
            self.inner_payload[0:3],
        )

    def test_locate_by_path(self) -> None:
        inner_leaf = self.archive.leaves()[1]
        # Requesting bytes 0..3 intersects the first full 3-byte group, so
        # the covered span expands to the group edge at offset 3.
        location = self.archive.locate_path(inner_leaf.path, 0, 4)
        self.assertEqual(
            location.path_string(),
            "root > part[1] > rfc822[0] > part[0]",
        )
        self.assertEqual((location.covered_start, location.covered_end), (0, 6))
        self.assertEqual(
            decode_located_bytes(self.archive, location),
            self.inner_payload[0:6],
        )

    def test_unknown_path_rejected(self) -> None:
        with self.assertRaises(InvalidSelectionError):
            self.archive.locate_path((), 0, 1)


class DuplicatesAndCorruptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw, self.shared_payload = duplicates_and_corruption_message()
        self.archive = MimeArchive(self.raw)

    def test_duplicate_attachments_are_distinct_instances(self) -> None:
        matches = self.archive.find_parts(filename="same.bin")
        self.assertEqual(len(matches), 2)
        first, second = matches
        self.assertIsNot(first, second)
        self.assertNotEqual(first.leaf_index, second.leaf_index)
        self.assertEqual(
            first.path_string(), "root > part[0]"
        )
        self.assertEqual(
            second.path_string(), "root > part[1]"
        )
        # Identical bytes, but raw ranges are separate physical locations.
        loc1 = self.archive.locate(first.leaf_index, 0, 5)
        loc2 = self.archive.locate(second.leaf_index, 0, 5)
        self.assertNotEqual(loc1.raw_ranges, loc2.raw_ranges)
        self.assertEqual(
            loc1.extract(self.archive.source),
            loc2.extract(self.archive.source),
        )
        self.assertEqual(
            decode_located_bytes(self.archive, loc1),
            self.shared_payload[0:6],
        )

    def test_corrupted_part_isolated_from_siblings(self) -> None:
        leaves = self.archive.leaves()
        # Order: healthy, healthy, corrupt, qp.
        corrupt_leaf = leaves[2]
        self.assertTrue(corrupt_leaf.is_corrupted)
        self.assertIn("invalid base64 character",
                      corrupt_leaf.corruption_reason)
        with self.assertRaises(CorruptedPartError):
            self.archive.locate(corrupt_leaf.leaf_index, 0, 1)

        # Both healthy siblings and the QP sibling still locate normally.
        for index in (0, 1, 3):
            leaf = leaves[index]
            self.assertFalse(leaf.is_corrupted)
            location = self.archive.locate(
                leaf.leaf_index, 0, min(5, leaf.decoded_length)
            )
            self.assertTrue(location.raw_ranges)

    def test_qp_sibling_after_corruption_round_trips(self) -> None:
        qp_leaf = self.archive.find_parts(filename="note.txt")[0]
        location = self.archive.locate(
            qp_leaf.leaf_index, 0, qp_leaf.decoded_length
        )
        self.assertEqual(
            decode_located_bytes(self.archive, location),
            b"soft\xe2\x80\x93break=here\r\nline two",
        )

    def test_leaf_indices_stable_depth_first(self) -> None:
        leaves = self.archive.leaves()
        self.assertEqual(
            [leaf.leaf_index for leaf in leaves], [0, 1, 2, 3]
        )


if __name__ == "__main__":
    unittest.main()
