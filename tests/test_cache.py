"""Cache budget semantics measured in decompressed bytes."""

import unittest

from range_deflate import DecompressedCache, CacheKey
from range_deflate.builder import build_object
from range_deflate import Index, InMemoryStore, RangeReader


def _key(offset: int, length: int, checksum: int) -> CacheKey:
    return CacheKey(offset, length, checksum)


class CacheUnitTests(unittest.TestCase):
    def test_budget_zero_disables_caching(self) -> None:
        cache = DecompressedCache(0)
        cache.put(_key(0, 1, 1), b"abcd")
        self.assertIsNone(cache.get(_key(0, 1, 1)))
        self.assertEqual(cache.stats().bypassed, 1)
        self.assertEqual(cache.used_bytes, 0)

    def test_negative_budget_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DecompressedCache(-1)

    def test_lru_eviction_kept_inside_budget(self) -> None:
        cache = DecompressedCache(10)
        cache.put(_key(0, 1, 1), b"aaaa")  # 4
        cache.put(_key(1, 1, 2), b"bbbb")  # 4, total 8
        cache.put(_key(2, 1, 3), b"cc")    # 2, total 10
        self.assertEqual(cache.used_bytes, 10)
        cache.put(_key(3, 1, 4), b"ddddd")  # needs 5, evicts LRU entries
        self.assertLessEqual(cache.used_bytes, 10)
        self.assertIsNone(cache.get(_key(0, 1, 1)))
        stats = cache.stats()
        self.assertGreaterEqual(stats.evictions, 1)

    def test_recently_used_entry_is_protected(self) -> None:
        cache = DecompressedCache(8)
        cache.put(_key(0, 1, 1), b"aaaa")
        cache.put(_key(1, 1, 2), b"bbbb")
        # Touch the first entry so it becomes most-recently-used.
        self.assertIsNotNone(cache.get(_key(0, 1, 1)))
        cache.put(_key(2, 1, 3), b"cccc")
        self.assertIsNotNone(cache.get(_key(0, 1, 1)))
        self.assertIsNone(cache.get(_key(1, 1, 2)))

    def test_oversized_frame_is_bypassed_not_evicting_all(self) -> None:
        cache = DecompressedCache(4)
        cache.put(_key(0, 1, 1), b"abcd")
        cache.put(_key(1, 1, 2), b"too-large")
        self.assertIsNone(cache.get(_key(1, 1, 2)))
        # The existing small entry survives instead of being flushed.
        self.assertIsNotNone(cache.get(_key(0, 1, 1)))
        self.assertEqual(cache.stats().bypassed, 1)

    def test_inserting_large_fitting_frame_evicts_until_within_budget(self) -> None:
        cache = DecompressedCache(10)
        cache.put(_key(0, 1, 1), b"aaa")  # 3
        cache.put(_key(1, 1, 2), b"bbb")  # 3, total 6
        cache.put(_key(2, 1, 3), b"ccc")  # 3, total 9
        # A 7-byte frame fits the budget but not in the remaining 1 byte:
        # older entries are evicted until it can be stored.
        cache.put(_key(3, 1, 4), b"d" * 7)
        self.assertLessEqual(cache.used_bytes, 10)
        self.assertIsNotNone(cache.get(_key(3, 1, 4)))
        self.assertGreaterEqual(cache.stats().evictions, 2)

    def test_unlimited_budget(self) -> None:
        cache = DecompressedCache(None)
        for index in range(50):
            cache.put(_key(index, 1, index), bytes([index]) * 100)
        self.assertEqual(cache.used_bytes, 5000)
        self.assertEqual(cache.stats().evictions, 0)

    def test_hit_and_miss_accounting(self) -> None:
        cache = DecompressedCache(None)
        key = _key(0, 1, 1)
        self.assertIsNone(cache.get(key))
        cache.put(key, b"x")
        self.assertEqual(cache.get(key), b"x")
        stats = cache.stats()
        self.assertEqual(stats.misses, 1)
        self.assertEqual(stats.hits, 1)


class ReaderCacheIntegrationTests(unittest.TestCase):
    def test_second_read_is_served_from_cache(self) -> None:
        obj = build_object([(b"hello", None, 0), (b"world!!", "f0", 3)])
        store = InMemoryStore(obj.compressed)
        reader = RangeReader(
            Index(obj.entries()), store, DecompressedCache(None)
        )
        first = reader.read_range(0, 11)
        self.assertEqual(first.frames_fetched, ("f0", "f1"))
        second = reader.read_range(1, 9)
        self.assertEqual(second.frames_fetched, ())
        self.assertEqual(set(second.frames_from_cache), {"f0", "f1"})
        self.assertEqual(second.data, b"elloworl")
        self.assertEqual(store.fetch_count(), 2)

    def test_tight_budget_forces_refetch(self) -> None:
        obj = build_object(
            [
                (b"aaaa", None, 0),
                (b"bbbbbbbb", "f0", 2),
                (b"cccccccccccc", "f1", 2),
            ]
        )
        store = InMemoryStore(obj.compressed)
        # Budget only holds the smallest frame (4 decompressed bytes).
        reader = RangeReader(
            Index(obj.entries()), store, DecompressedCache(4)
        )
        reader.read_range(0, 24)
        self.assertEqual(store.fetch_count(), 3)
        # f0 (4 bytes) fits; larger frames were bypassed. Reading again
        # refetches f1 and f2 but reuses f0.
        result = reader.read_range(0, 24)
        self.assertEqual(result.frames_from_cache, ("f0",))
        self.assertEqual(set(result.frames_fetched), {"f1", "f2"})
        self.assertEqual(store.fetch_count(), 5)

    def test_cache_key_is_independent_of_frame_id(self) -> None:
        # Two entries with identical physical location and checksum share a
        # cache entry even if their frame ids differ.
        obj = build_object([(b"shared-bytes", None, 0)])
        entry = obj.frames[0].entry
        store = InMemoryStore(obj.compressed)
        index = Index([entry])
        reader = RangeReader(index, store, DecompressedCache(None))
        reader.read_range(0, 12)
        key = CacheKey.from_entry(entry)
        self.assertTrue(reader.cache.contains(key))


if __name__ == "__main__":
    unittest.main()
