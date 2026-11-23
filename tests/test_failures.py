"""Failure handling: truncation, checksum mismatch, wrong dictionary.

The contract is strict: whenever any frame in a dependency chain fails, the
reader raises an error naming the exact frame and never returns partial
range bytes.
"""

import unittest
import zlib
from dataclasses import replace

from range_deflate import (
    DecompressedCache,
    CacheKey,
    FrameChecksumError,
    FrameEntry,
    FrameTruncatedError,
    Index,
    InMemoryStore,
    RangeReader,
)
from range_deflate.builder import (
    build_object,
    encode_dictionary_frame,
    encode_literal_frame,
)


def _dictionary_dependent_fixture():
    """Three frames where later frames back-reference reset dictionaries.

    The declared ``dict_size`` always matches the dictionary the stream was
    actually encoded against, so the healthy fixture inflates end to end.
    """
    dictionary = b"ABCDEFGHIJKLMNOP"
    f0_payload = dictionary
    f0_stream = encode_literal_frame(f0_payload)

    # f1 reaches 8 bytes back into the full 16-byte f0 output.
    f1_stream, f1_payload = encode_dictionary_frame(
        dictionary, match_length=4, distance=8, suffix=b"tail-f1"
    )
    # f2 reaches 6 bytes back into f1's tail.
    f2_dictionary = f1_payload[-6:]
    f2_stream, f2_payload = encode_dictionary_frame(
        f2_dictionary,
        match_length=3,
        distance=6,
        suffix=b"end",
    )

    streams = [f0_stream, f1_stream, f2_stream]
    payloads = [f0_payload, f1_payload, f2_payload]
    dict_sizes = [0, len(dictionary), len(f2_dictionary)]
    entries = []
    offset = 0
    logical = 0
    for index, (stream, payload) in enumerate(zip(streams, payloads)):
        entries.append(
            FrameEntry(
                frame_id=f"f{index}",
                logical_start=logical,
                logical_end=logical + len(payload),
                compressed_offset=offset,
                compressed_length=len(stream),
                checksum=zlib.crc32(payload) & 0xFFFFFFFF,
                dict_frame_id=None if index == 0 else f"f{index - 1}",
                dict_size=dict_sizes[index],
            )
        )
        offset += len(stream)
        logical += len(payload)

    compressed = b"".join(streams)
    full = b"".join(payloads)
    return compressed, tuple(entries), full


class DictionaryDependentReadTests(unittest.TestCase):
    def test_dependent_chain_reads_correctly(self) -> None:
        compressed, entries, full = _dictionary_dependent_fixture()
        reader = RangeReader(
            Index(entries), InMemoryStore(compressed), DecompressedCache(None)
        )
        result = reader.read_range(0, len(full))
        self.assertEqual(result.data, full)
        self.assertEqual(result.frames_needed, ("f0", "f1", "f2"))

    def test_last_frame_requires_every_predecessor(self) -> None:
        compressed, entries, full = _dictionary_dependent_fixture()
        store = InMemoryStore(compressed)
        reader = RangeReader(Index(entries), store, DecompressedCache(None))
        last = entries[-1]
        result = reader.read_range(last.logical_start, last.logical_end)
        self.assertEqual(result.data, full[last.logical_start :])
        self.assertEqual(store.fetched_frame_ids(), ("f0", "f1", "f2"))


class TruncationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compressed, self.entries, self.full = _dictionary_dependent_fixture()

    def _reader_with_store(self, store: InMemoryStore) -> RangeReader:
        return RangeReader(
            Index(self.entries), store, DecompressedCache(None)
        )

    def test_short_store_names_the_truncated_frame(self) -> None:
        # Cut the store in the middle of f1's compressed bytes.
        f1 = self.entries[1]
        cut = f1.compressed_offset + f1.compressed_length - 1
        reader = self._reader_with_store(InMemoryStore(self.compressed[:cut]))
        with self.assertRaises(FrameTruncatedError) as ctx:
            reader.read_range(0, len(self.full))
        self.assertEqual(ctx.exception.frame_id, "f1")

    def test_truncated_dependency_frame_is_named_not_the_target(self) -> None:
        f0 = self.entries[0]
        cut = f0.compressed_offset + f0.compressed_length - 1
        reader = self._reader_with_store(InMemoryStore(self.compressed[:cut]))
        with self.assertRaises(FrameTruncatedError) as ctx:
            # Asking only for f2 still requires f0; f0's failure must surface.
            reader.read_range(self.entries[2].logical_start, len(self.full))
        self.assertEqual(ctx.exception.frame_id, "f0")

    def test_truncated_final_block_marker_is_checksum_failure(self) -> None:
        # Overwrite (not delete) the final frame's last byte so every
        # compressed offset stays valid, but f2's own stream loses its
        # end-of-block marker. eof stays False and the reader reports a
        # checksum-class failure attributed to f2.
        f2 = self.entries[2]
        damaged = bytearray(self.compressed)
        position = f2.compressed_offset + f2.compressed_length - 1
        damaged[position] ^= 0xFF
        reader = RangeReader(
            Index(self.entries),
            InMemoryStore(bytes(damaged)),
            DecompressedCache(None),
        )
        with self.assertRaises(FrameChecksumError) as ctx:
            reader.read_range(0, len(self.full))
        self.assertEqual(ctx.exception.frame_id, "f2")

    def test_no_partial_data_on_failure(self) -> None:
        f1 = self.entries[1]
        cut = f1.compressed_offset + 1
        reader = self._reader_with_store(InMemoryStore(self.compressed[:cut]))
        with self.assertRaises(FrameTruncatedError):
            reader.read_range(0, len(self.full))
        # The dependency f0 was cached before f1 failed, but f1 itself never
        # entered the cache: the failed frame produced nothing reusable.
        cached_keys = {
            CacheKey.from_entry(entry): entry.frame_id
            for entry in self.entries
        }
        cached_frames = {
            frame_id
            for key, frame_id in cached_keys.items()
            if reader.cache.contains(key)
        }
        self.assertIn("f0", cached_frames)
        self.assertNotIn("f1", cached_frames)
        self.assertNotIn("f2", cached_frames)


class ChecksumTests(unittest.TestCase):
    def test_wrong_recorded_checksum_names_frame(self) -> None:
        compressed, entries, full = _dictionary_dependent_fixture()
        damaged = tuple(
            replace(entry, checksum=entry.checksum ^ 0xDEADBEEF)
            if entry.frame_id == "f1"
            else entry
            for entry in entries
        )
        reader = RangeReader(
            Index(damaged), InMemoryStore(compressed), DecompressedCache(None)
        )
        with self.assertRaises(FrameChecksumError) as ctx:
            reader.read_range(0, len(full))
        self.assertEqual(ctx.exception.frame_id, "f1")

    def test_corrupted_compressed_byte_named(self) -> None:
        compressed, entries, full = _dictionary_dependent_fixture()
        damaged = bytearray(compressed)
        f1 = entries[1]
        damaged[f1.compressed_offset + 1] ^= 0xFF
        reader = RangeReader(
            Index(entries), InMemoryStore(bytes(damaged)),
            DecompressedCache(None),
        )
        with self.assertRaises(FrameChecksumError) as ctx:
            reader.read_range(0, len(full))
        self.assertEqual(ctx.exception.frame_id, "f1")

    def test_wrong_length_in_index_named(self) -> None:
        compressed, entries, full = _dictionary_dependent_fixture()
        f1 = entries[1]
        damaged = tuple(
            replace(entry, logical_end=entry.logical_end + 1)
            if entry.frame_id == "f1"
            else entry
            for entry in entries
        )
        # A changed logical end breaks contiguity, so instead patch via a
        # rebuilt index: validate direct inflation error path separately.
        with self.assertRaises(Exception):
            Index(damaged)
        reader = RangeReader(
            Index(entries), InMemoryStore(compressed), DecompressedCache(None)
        )
        # Direct check: inflate with a deliberately wrong expected length.
        from range_deflate.inflate import inflate_frame

        with self.assertRaises(FrameChecksumError) as ctx:
            inflate_frame(
                "f1",
                compressed[f1.compressed_offset : f1.compressed_offset
                           + f1.compressed_length],
                expected_checksum=f1.checksum,
                expected_length=f1.logical_length + 1,
                dictionary=b"ABCDEFGH"[-entries[1].dict_size:],
            )
        self.assertEqual(ctx.exception.frame_id, "f1")

    def test_trailing_garbage_rejected(self) -> None:
        compressed, entries, _ = _dictionary_dependent_fixture()
        f0 = entries[0]
        stream = compressed[
            f0.compressed_offset : f0.compressed_offset + f0.compressed_length
        ]
        from range_deflate.inflate import inflate_frame

        with self.assertRaises(FrameChecksumError):
            inflate_frame(
                "f0",
                stream + b"\x00\x00",
                expected_checksum=f0.checksum,
                expected_length=f0.logical_length,
            )


class WrongDictionaryTests(unittest.TestCase):
    def test_index_pointing_at_wrong_predecessor_fails_checksum(self) -> None:
        compressed, entries, full = _dictionary_dependent_fixture()
        # Repoint f1's dictionary to a too-short tail of f0 so the inflated
        # bytes differ from the recorded checksum.
        f1 = entries[1]
        repointed = tuple(
            replace(entry, dict_size=1) if entry.frame_id == "f1" else entry
            for entry in entries
        )
        reader = RangeReader(
            Index(repointed), InMemoryStore(compressed), DecompressedCache(None)
        )
        with self.assertRaises(FrameChecksumError) as ctx:
            reader.read_range(0, len(full))
        self.assertEqual(ctx.exception.frame_id, "f1")

    def test_builder_fixture_invalid_without_dictionary(self) -> None:
        dictionary = b"ABCDEFGH"
        stream, expected = encode_dictionary_frame(dictionary, 3, 8)
        decompressor = zlib.decompressobj(-15)
        with self.assertRaises(zlib.error):
            decompressor.decompress(stream)


class StoredObjectFailureTests(unittest.TestCase):
    def test_stored_frame_truncation_path(self) -> None:
        obj = build_object([(b"hello", None, 0), (b"world", "f0", 3)])
        cut = obj.frames[1].entry.compressed_offset + 1
        reader = RangeReader(
            Index(obj.entries()),
            InMemoryStore(obj.compressed[:cut]),
            DecompressedCache(None),
        )
        with self.assertRaises(FrameTruncatedError) as ctx:
            reader.read_range(0, 10)
        self.assertEqual(ctx.exception.frame_id, "f1")


if __name__ == "__main__":
    unittest.main()
