"""Structural validation and append-only versioning of the index."""

import unittest

from range_deflate import FrameEntry, Index, InvalidIndexError


def _entry(
    frame_id: str,
    start: int,
    end: int,
    *,
    checksum: int = 0,
    dict_frame_id: str | None = None,
    dict_size: int = 0,
    offset: int | None = None,
) -> FrameEntry:
    return FrameEntry(
        frame_id=frame_id,
        logical_start=start,
        logical_end=end,
        compressed_offset=start if offset is None else offset,
        compressed_length=10,
        checksum=checksum,
        dict_frame_id=dict_frame_id,
        dict_size=dict_size,
    )


class IndexValidationTests(unittest.TestCase):
    def test_valid_contiguous_index(self) -> None:
        index = Index([_entry("a", 0, 4), _entry("b", 4, 9)])
        self.assertEqual(index.latest_version, 0)
        self.assertEqual(len(index), 1)

    def test_empty_version_rejected(self) -> None:
        with self.assertRaises(InvalidIndexError):
            Index([])

    def test_duplicate_frame_id_rejected(self) -> None:
        with self.assertRaises(InvalidIndexError):
            Index([_entry("a", 0, 4), _entry("a", 4, 8)])

    def test_logical_gap_rejected(self) -> None:
        with self.assertRaises(InvalidIndexError):
            Index([_entry("a", 0, 4), _entry("b", 5, 9)])

    def test_logical_overlap_rejected(self) -> None:
        with self.assertRaises(InvalidIndexError):
            Index([_entry("a", 0, 5), _entry("b", 4, 9)])

    def test_first_frame_must_start_at_zero(self) -> None:
        with self.assertRaises(InvalidIndexError):
            Index([_entry("a", 1, 5)])

    def test_non_positive_logical_range_rejected(self) -> None:
        with self.assertRaises(InvalidIndexError):
            Index([_entry("a", 0, 0)])
        with self.assertRaises(InvalidIndexError):
            Index([_entry("a", 4, 3)])

    def test_dictionary_frame_must_exist(self) -> None:
        with self.assertRaises(InvalidIndexError):
            Index([_entry("a", 0, 4, dict_frame_id="ghost", dict_size=2)])

    def test_dictionary_size_mismatch_with_absence(self) -> None:
        with self.assertRaises(InvalidIndexError):
            Index([_entry("a", 0, 4, dict_size=3)])
        with self.assertRaises(InvalidIndexError):
            Index(
                [
                    _entry("a", 0, 4),
                    _entry("b", 4, 8, dict_frame_id="a", dict_size=0),
                ]
            )

    def test_dictionary_larger_than_predecessor_rejected(self) -> None:
        with self.assertRaises(InvalidIndexError):
            Index(
                [
                    _entry("a", 0, 3),
                    _entry("b", 3, 7, dict_frame_id="a", dict_size=4),
                ]
            )

    def test_dependency_cycle_rejected(self) -> None:
        with self.assertRaises(InvalidIndexError):
            Index(
                [
                    _entry("a", 0, 4, dict_frame_id="b", dict_size=2),
                    _entry("b", 4, 8, dict_frame_id="a", dict_size=2),
                ]
            )

    def test_self_dependency_rejected(self) -> None:
        with self.assertRaises(InvalidIndexError):
            Index([_entry("a", 0, 4, dict_frame_id="a", dict_size=2)])

    def test_boolean_and_negative_fields_rejected(self) -> None:
        with self.assertRaises(InvalidIndexError):
            Index(
                [
                    FrameEntry(
                        frame_id="a",
                        logical_start=True,  # type: ignore[arg-type]
                        logical_end=4,
                        compressed_offset=0,
                        compressed_length=1,
                        checksum=0,
                    )
                ]
            )
        with self.assertRaises(InvalidIndexError):
            Index(
                [
                    FrameEntry(
                        frame_id="a",
                        logical_start=0,
                        logical_end=4,
                        compressed_offset=-1,
                        compressed_length=1,
                        checksum=0,
                    )
                ]
            )
        with self.assertRaises(InvalidIndexError):
            Index(
                [
                    FrameEntry(
                        frame_id="a",
                        logical_start=0,
                        logical_end=4,
                        compressed_offset=0,
                        compressed_length=0,
                        checksum=0,
                    )
                ]
            )

    def test_checksum_must_be_32_bit(self) -> None:
        with self.assertRaises(InvalidIndexError):
            Index(
                [
                    FrameEntry(
                        frame_id="a",
                        logical_start=0,
                        logical_end=4,
                        compressed_offset=0,
                        compressed_length=1,
                        checksum=1 << 32,
                    )
                ]
            )


class VersioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = Index([_entry("a", 0, 4)])

    def test_append_publishes_immutable_snapshots(self) -> None:
        first = self.index.get_version(0)
        self.assertEqual(first.version, 0)
        second = self.index.append_version(
            [_entry("a", 0, 4), _entry("b", 4, 10)]
        )
        self.assertEqual(second.version, 1)
        # The first snapshot keeps its original single entry.
        self.assertEqual(len(first.entries), 1)
        self.assertEqual(len(self.index.get_version(0).entries), 1)
        self.assertEqual(len(self.index.get_version(1).entries), 2)
        self.assertEqual(self.index.latest_version, 1)

    def test_latest_default_resolves_newest(self) -> None:
        self.index.append_version([_entry("a", 0, 6)])
        self.assertEqual(self.index.get_version().version, 1)

    def test_unknown_version_rejected(self) -> None:
        with self.assertRaises(InvalidIndexError):
            self.index.get_version(7)
        with self.assertRaises(InvalidIndexError):
            self.index.get_version(-1)

    def test_empty_index_has_no_version(self) -> None:
        index = Index.__new__(Index)
        Index.__init__(index)
        with self.assertRaises(InvalidIndexError):
            index.latest_version
        with self.assertRaises(InvalidIndexError):
            index.get_version()


if __name__ == "__main__":
    unittest.main()
