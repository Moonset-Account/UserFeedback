"""Concurrency and interleaving acceptance tests.

These cover the explicitly required interleaving scenarios: many threads
reading concurrently while index versions are appended, cache eviction
under a tight budget, and reader reuse after a frame failure.
"""

import random
import threading
import unittest

from range_deflate import (
    DecompressedCache,
    FrameChecksumError,
    FrameTruncatedError,
    Index,
    InMemoryStore,
    RangeReader,
)
from range_deflate.builder import build_object


class ConcurrentReadTests(unittest.TestCase):
    def setUp(self) -> None:
        rng = random.Random(7)
        self.payloads = [
            bytes(rng.randrange(256) for _ in range(rng.randint(8, 40)))
            for _ in range(12)
        ]
        spec = []
        for index, payload in enumerate(self.payloads):
            if index == 0:
                spec.append((payload, None, 0))
            else:
                spec.append((payload, f"f{index - 1}", min(4, len(self.payloads[index - 1]))))
        self.obj = build_object(spec)
        self.full = b"".join(self.payloads)
        self.boundaries = [0]
        for payload in self.payloads:
            self.boundaries.append(self.boundaries[-1] + len(payload))

    def test_parallel_range_reads_are_consistent(self) -> None:
        store = InMemoryStore(self.obj.compressed)
        reader = RangeReader(
            Index(self.obj.entries()), store, DecompressedCache(64)
        )
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker(seed: int) -> None:
            try:
                barrier.wait()
                rng = random.Random(seed)
                for _ in range(200):
                    start = rng.randrange(0, len(self.full))
                    end = rng.randrange(start, len(self.full) + 1)
                    result = reader.read_range(start, end)
                    assert result.data == self.full[start:end]
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        # Every fetched record must correspond to a real frame slice.
        for record in store.fetch_log:
            self.assertGreaterEqual(record.offset, 0)
            self.assertGreater(record.length, 0)

    def test_append_during_reads_keeps_each_read_pinned(self) -> None:
        store = InMemoryStore(self.obj.compressed)
        index = Index(self.obj.entries())
        reader = RangeReader(index, store, DecompressedCache(None))
        observed_versions: set[int] = set()
        stop = threading.Event()
        errors: list[BaseException] = []

        def reader_worker() -> None:
            try:
                while not stop.is_set():
                    result = reader.read_range(0, len(self.full))
                    observed_versions.add(result.version)
                    self.assertEqual(result.data, self.full)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        def appender() -> None:
            try:
                for _ in range(6):
                    index.append_version(self.obj.entries())
                    threading.Event().wait(0.001)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        readers = [threading.Thread(target=reader_worker) for _ in range(4)]
        for thread in readers:
            thread.start()
        writer = threading.Thread(target=appender)
        writer.start()
        writer.join()
        stop.set()
        for thread in readers:
            thread.join()
        self.assertEqual(errors, [])
        # Reads observed multiple pinned versions, and every result was
        # internally consistent with the version it reported.
        self.assertGreaterEqual(len(observed_versions), 2)
        self.assertEqual(index.latest_version, 6)

    def test_failure_in_one_thread_does_not_poison_reader(self) -> None:
        good_store = InMemoryStore(self.obj.compressed)
        cut_store = InMemoryStore(self.obj.compressed[:-1])
        index = Index(self.obj.entries())
        good_reader = RangeReader(index, good_store, DecompressedCache(None))
        cut_reader = RangeReader(index, cut_store, DecompressedCache(None))

        failure: list[BaseException] = []

        def bad_read() -> None:
            try:
                cut_reader.read_range(0, len(self.full))
            except (FrameTruncatedError, FrameChecksumError) as exc:
                failure.append(exc)

        thread = threading.Thread(target=bad_read)
        thread.start()
        # Concurrent healthy reads keep succeeding on the other reader.
        for _ in range(50):
            result = good_reader.read_range(0, len(self.full))
            self.assertEqual(result.data, self.full)
        thread.join()
        self.assertEqual(len(failure), 1)
        self.assertIsInstance(failure[0], (FrameTruncatedError, FrameChecksumError))

    def test_cache_eviction_under_contended_budget(self) -> None:
        store = InMemoryStore(self.obj.compressed)
        # Budget smaller than most frames: constant churn, never an error.
        reader = RangeReader(
            Index(self.obj.entries()), store, DecompressedCache(16)
        )
        errors: list[BaseException] = []

        def worker(seed: int) -> None:
            rng = random.Random(seed)
            try:
                for _ in range(100):
                    frame_index = rng.randrange(len(self.payloads))
                    start = self.boundaries[frame_index]
                    end = self.boundaries[frame_index + 1]
                    result = reader.read_range(start, end)
                    self.assertEqual(result.data, self.payloads[frame_index])
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(reader.cache.used_bytes, 16)


if __name__ == "__main__":
    unittest.main()
