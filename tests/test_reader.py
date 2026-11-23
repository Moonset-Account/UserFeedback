"""Normal and boundary range-read behaviour."""

import unittest

from range_deflate import (
    DecompressedCache,
    FrameEntry,
    Index,
    InMemoryStore,
    RangeOutOfBoundsError,
    RangeReader,
)
from range_deflate.builder import build_object
import zlib


def _three_frame_object() -> tuple[bytes, tuple[FrameEntry, ...], bytes]:
    payloads = [b"AAAA", b"BBBBBB", b"CCCCCCCC"]
    obj = build_object(
        [
            (payloads[0], None, 0),
            (payloads[1], "f0", 3),
            (payloads[2], "f1", 4),
        ]
    )
    full = b"".join(payloads)
    return obj.compressed, obj.entries(), full


class RangeReadTests(unittest.TestCase):
    def setUp(self) -> None:
        compressed, entries, self.full = _three_frame_object()
        self.store = InMemoryStore(compressed)
        self.index = Index(entries)
        self.reader = RangeReader(self.index, self.store, DecompressedCache(None))

    def test_read_entire_object(self) -> None:
        result = self.reader.read_range(0, len(self.full))
        self.assertEqual(result.data, self.full)
        self.assertEqual(result.logical_coverage, ("f0", "f1", "f2"))
        self.assertEqual(result.frames_needed, ("f0", "f1", "f2"))
        self.assertEqual(result.frames_fetched, ("f0", "f1", "f2"))
        self.assertEqual(result.frames_from_cache, ())

    def test_subrange_spanning_two_frames_is_sliced_correctly(self) -> None:
        result = self.reader.read_range(2, 12)
        self.assertEqual(result.data, self.full[2:12])
        self.assertEqual(result.logical_coverage, ("f0", "f1", "f2"))

    def test_range_inside_single_frame(self) -> None:
        result = self.reader.read_range(5, 8)
        self.assertEqual(result.data, self.full[5:8])
        self.assertEqual(result.logical_coverage, ("f1",))
        # f1 depends on f0's dictionary, so f0 is materialised but not in
        # the logical coverage.
        self.assertEqual(result.frames_needed, ("f0", "f1"))
        self.assertEqual(set(result.frames_fetched), {"f0", "f1"})

    def test_reading_last_frame_pulls_full_dependency_chain(self) -> None:
        result = self.reader.read_range(10, 18)
        self.assertEqual(result.data, self.full[10:18])
        self.assertEqual(result.frames_needed, ("f0", "f1", "f2"))
        self.assertEqual(result.logical_coverage, ("f2",))

    def test_boundary_offsets(self) -> None:
        # Zero-length slice at a frame boundary returns no bytes.
        result = self.reader.read_range(4, 4)
        self.assertEqual(result.data, b"")
        self.assertEqual(result.frames_needed, ())
        # First and last single bytes.
        self.assertEqual(self.reader.read_range(0, 1).data, self.full[:1])
        self.assertEqual(self.reader.read_range(17, 18).data, self.full[-1:])
        # Open-ended end reads to the object tail.
        self.assertEqual(self.reader.read_range(10).data, self.full[10:])

    def test_out_of_bounds_ranges(self) -> None:
        with self.assertRaises(RangeOutOfBoundsError):
            self.reader.read_range(-1, 4)
        with self.assertRaises(RangeOutOfBoundsError):
            self.reader.read_range(0, len(self.full) + 1)
        with self.assertRaises(RangeOutOfBoundsError):
            self.reader.read_range(len(self.full) + 1, len(self.full) + 2)
        with self.assertRaises(RangeOutOfBoundsError):
            self.reader.read_range(5, 3)

    def test_caller_can_audit_physical_fetches(self) -> None:
        self.reader.read_range(10, 14)  # only touches f2 logically
        self.assertEqual(self.store.fetched_frame_ids(), ("f0", "f1", "f2"))
        records = self.store.fetch_log
        self.assertTrue(all(record.length > 0 for record in records))
        # Second read is fully cached: no new physical fetches.
        self.store.reset_fetch_log()
        result = self.reader.read_range(0, 18)
        self.assertEqual(self.store.fetch_log, ())
        self.assertEqual(result.frames_fetched, ())
        self.assertEqual(set(result.frames_from_cache), {"f0", "f1", "f2"})

    def test_single_byte_object(self) -> None:
        obj = build_object([(b"Z", None, 0)])
        reader = RangeReader(
            Index(obj.entries()),
            InMemoryStore(obj.compressed),
            DecompressedCache(None),
        )
        self.assertEqual(reader.read_range(0, 1).data, b"Z")
        self.assertEqual(reader.read_range(0, 0).data, b"")

    def test_stored_frame_checksum_covers_full_payload(self) -> None:
        # Sanity: the builder records real CRC-32 values.
        _, entries, _ = _three_frame_object()
        payload = b"AAAA"
        self.assertEqual(entries[0].checksum, zlib.crc32(payload) & 0xFFFFFFFF)

    def test_compressed_offsets_point_at_distinct_slices(self) -> None:
        compressed, entries, _ = _three_frame_object()
        for entry in entries:
            slice_ = compressed[
                entry.compressed_offset : entry.compressed_offset
                + entry.compressed_length
            ]
            self.assertEqual(len(slice_), entry.compressed_length)
            # Each stored slice inflates back to a non-empty payload.
            decompressor = zlib.decompressobj(-15)
            self.assertTrue(decompressor.decompress(slice_))


class CustomStoreBackendTests(unittest.TestCase):
    def test_generic_compressed_store_protocol_is_supported(self) -> None:
        compressed, entries, full = _three_frame_object()

        class CallbackStore:
            def __init__(self, data: bytes) -> None:
                self.data = data
                self.calls = 0

            def read(self, offset: int, length: int) -> bytes:
                self.calls += 1
                return self.data[offset : offset + length]

        backend = CallbackStore(compressed)
        reader = RangeReader(Index(entries), backend, DecompressedCache(None))
        result = reader.read_range(0, len(full))
        self.assertEqual(result.data, full)
        self.assertEqual(backend.calls, 3)

    def test_backend_exception_is_wrapped_as_named_truncation(self) -> None:
        compressed, entries, full = _three_frame_object()

        class FailingStore:
            def read(self, offset: int, length: int) -> bytes:
                raise OSError("simulated backend outage")

        reader = RangeReader(
            Index(entries), FailingStore(), DecompressedCache(None)
        )
        from range_deflate import FrameTruncatedError

        with self.assertRaises(FrameTruncatedError) as ctx:
            reader.read_range(0, len(full))
        self.assertEqual(ctx.exception.frame_id, "f0")


if __name__ == "__main__":
    unittest.main()
