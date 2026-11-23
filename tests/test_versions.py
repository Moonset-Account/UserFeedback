"""Two index versions over identical compressed bytes.

The acceptance scenario: version 1 and version 2 reference the *same* raw
DEFLATE byte string but declare different reset dictionaries. Because the
frames are stored blocks with no back-references, both versions inflate to
the same logical bytes. The tests verify:

* both versions' range reads return correct data,
* a read pins one version for its whole duration,
* a frame cached under the older version keeps serving reads, including
  cross-version cache hits,
* and a genuinely dictionary-dependent frame fails when an index version
  mis-declares its dictionary.
"""

import unittest
import zlib
from dataclasses import replace

from range_deflate import (
    DecompressedCache,
    FrameChecksumError,
    FrameEntry,
    Index,
    InMemoryStore,
    RangeReader,
)
from range_deflate.builder import (
    build_object,
    encode_dictionary_frame,
    encode_literal_frame,
    encode_stored_frame,
)


class StoredBlockTwoVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        # Stored blocks: dictionary-independent compressed bytes.
        self.payloads = [b"frame-zero-payload", b"frame-one-payload!!"]
        streams = [encode_stored_frame(payload) for payload in self.payloads]
        self.compressed = b"".join(streams)

        def make_entries(dict_zero: int, dict_one: int) -> tuple[FrameEntry, ...]:
            entries = []
            offset = 0
            logical = 0
            for index, (stream, payload) in enumerate(zip(streams, self.payloads)):
                dep = None if index == 0 else "f0"
                size = 0 if index == 0 else (dict_zero if index == 1 else dict_one)
                entries.append(
                    FrameEntry(
                        frame_id=f"f{index}",
                        logical_start=logical,
                        logical_end=logical + len(payload),
                        compressed_offset=offset,
                        compressed_length=len(stream),
                        checksum=zlib.crc32(payload) & 0xFFFFFFFF,
                        dict_frame_id=dep,
                        dict_size=size,
                    )
                )
                offset += len(stream)
                logical += len(payload)
            return tuple(entries)

        self.v0_entries = make_entries(dict_zero=4, dict_one=6)
        self.v1_entries = make_entries(dict_zero=9, dict_one=3)
        # Physical locations and checksums are identical across versions;
        # only the declared dictionary sizes differ (on frame f1, which is
        # the frame that actually carries a dictionary reference).
        for a, b in zip(self.v0_entries, self.v1_entries):
            self.assertEqual(a.compressed_offset, b.compressed_offset)
            self.assertEqual(a.compressed_length, b.compressed_length)
            self.assertEqual(a.checksum, b.checksum)
        self.assertEqual(self.v0_entries[0].dict_size, 0)
        self.assertEqual(self.v1_entries[0].dict_size, 0)
        self.assertNotEqual(
            self.v0_entries[1].dict_size, self.v1_entries[1].dict_size
        )

        self.index = Index(self.v0_entries)
        self.index.append_version(self.v1_entries)
        self.store = InMemoryStore(self.compressed)
        self.cache = DecompressedCache(None)
        self.reader = RangeReader(self.index, self.store, self.cache)

    def test_both_versions_read_correct_ranges(self) -> None:
        full = b"".join(self.payloads)
        for version in (0, 1):
            result = self.reader.read_range(2, len(full) - 2, version=version)
            self.assertEqual(result.version, version)
            self.assertEqual(result.data, full[2 : len(full) - 2])
            self.assertEqual(result.frames_needed, ("f0", "f1"))

    def test_pinned_session_ignores_later_appends(self) -> None:
        session = self.reader.session(version=0)
        self.assertEqual(session.version, 0)
        # Append a third version while the session is alive.
        self.index.append_version(self.v0_entries)
        result = session.read_range(0, 10)
        self.assertEqual(result.version, 0)
        self.assertEqual(result.data, b"".join(self.payloads)[:10])
        session.close()
        # The reader's default latest now points at version 2.
        self.assertEqual(self.reader.read_range(0, 1).version, 2)

    def test_older_version_cache_serves_newer_version(self) -> None:
        # Populate the cache exclusively through the older version.
        old = self.reader.read_range(0, len(b"".join(self.payloads)), version=0)
        self.assertEqual(old.frames_fetched, ("f0", "f1"))
        self.assertEqual(self.store.fetch_count(), 2)

        # The newer version must hit the same validated cache entries.
        new = self.reader.read_range(5, len(b"".join(self.payloads)) - 2, version=1)
        self.assertEqual(new.frames_fetched, ())
        self.assertEqual(set(new.frames_from_cache), {"f0", "f1"})
        self.assertEqual(self.store.fetch_count(), 2)
        self.assertEqual(
            new.data, b"".join(self.payloads)[5:35]
        )
        stats = self.cache.stats()
        self.assertEqual(stats.hits, 2)
        self.assertEqual(stats.misses, 2)

    def test_interleaved_versions_share_cache_entries(self) -> None:
        full = b"".join(self.payloads)
        # First read only covers f0, so just one physical fetch happens.
        first = self.reader.read_range(0, 18, version=0)
        self.assertEqual(self.store.fetch_count(), 1)
        self.assertEqual(first.data, full[:18])
        # The next read spans both frames: f1 is fetched once, f0 is a hit.
        spanning = self.reader.read_range(1, len(full) - 1, version=1)
        self.assertEqual(spanning.frames_fetched, ("f1",))
        self.assertEqual(self.store.fetch_count(), 2)
        # All further interleaved reads are pure cache hits on both versions.
        for version in (1, 0, 1, 0):
            result = self.reader.read_range(1, len(full) - 1, version=version)
            self.assertEqual(result.version, version)
            self.assertEqual(result.frames_fetched, ())
        self.assertEqual(self.store.fetch_count(), 2)

    def test_explicit_unknown_version_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.reader.read_range(0, 1, version=99)


class DictionaryDependentTwoVersionTests(unittest.TestCase):
    def test_different_dictionary_on_dependent_frame_breaks_checksum(self) -> None:
        # A fixed-Huffman frame that genuinely references dictionary bytes.
        real_dictionary = b"ABCDEFGH"
        stream, expected = encode_dictionary_frame(
            real_dictionary, match_length=3, distance=8, suffix=b"tail"
        )
        base_stream = encode_literal_frame(real_dictionary)
        compressed = base_stream + stream

        def entry(dict_size: int) -> tuple[FrameEntry, FrameEntry]:
            f0 = FrameEntry(
                frame_id="f0",
                logical_start=0,
                logical_end=len(real_dictionary),
                compressed_offset=0,
                compressed_length=len(base_stream),
                checksum=zlib.crc32(real_dictionary) & 0xFFFFFFFF,
            )
            f1 = FrameEntry(
                frame_id="f1",
                logical_start=len(real_dictionary),
                logical_end=len(real_dictionary) + len(expected),
                compressed_offset=len(base_stream),
                compressed_length=len(stream),
                checksum=zlib.crc32(expected) & 0xFFFFFFFF,
                dict_frame_id="f0",
                dict_size=dict_size,
            )
            return f0, f1

        good = entry(dict_size=8)
        # Same compressed bytes, but version 1 declares a 3-byte dictionary.
        bad = (good[0], replace(good[1], dict_size=3))
        index = Index(good)
        index.append_version(bad)
        store = InMemoryStore(compressed)

        # Version 0 with its own cache inflates correctly.
        good_reader = RangeReader(index, store, DecompressedCache(None))
        ok = good_reader.read_range(
            0, len(real_dictionary) + len(expected), version=0
        )
        self.assertEqual(ok.data, real_dictionary + expected)

        # The mis-declared version must fail when inflated from scratch
        # (a fresh cache, so the wrong 3-byte dictionary is actually used).
        bad_reader = RangeReader(index, store, DecompressedCache(None))
        with self.assertRaises(FrameChecksumError) as ctx:
            bad_reader.read_range(
                len(real_dictionary),
                len(real_dictionary) + len(expected),
                version=1,
            )
        self.assertEqual(ctx.exception.frame_id, "f1")

    def test_shared_bytes_with_wrong_checksum_are_not_cached(self) -> None:
        obj = build_object([(b"abcdefgh", None, 0)])
        good = obj.entries()
        bad = tuple(replace(e, checksum=e.checksum ^ 1) for e in good)
        index = Index(good)
        index.append_version(bad)
        cache = DecompressedCache(None)
        reader = RangeReader(index, InMemoryStore(obj.compressed), cache)
        reader.read_range(0, 8, version=0)
        self.assertEqual(cache.stats().entries, 1)
        # The bad version has a different CacheKey (checksum differs) and
        # fails without poisoning the good version's cached payload.
        with self.assertRaises(FrameChecksumError):
            reader.read_range(0, 8, version=1)
        again = reader.read_range(0, 8, version=0)
        self.assertEqual(again.frames_fetched, ())
        self.assertEqual(again.data, b"abcdefgh")


if __name__ == "__main__":
    unittest.main()
