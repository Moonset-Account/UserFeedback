from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from buffer_scheduler import (  # noqa: E402
    AccessMode,
    BufferScheduler,
    ModelLayout,
    RequestState,
    StageRequest,
)


class ThreadSafetyTests(unittest.TestCase):
    def test_concurrent_reader_completions_release_writer_once(self):
        n = 16
        scheduler = BufferScheduler(8192, model=ModelLayout("thread", alignment=64))
        scheduler.submit_stage(StageRequest("asr", (8, 8), ["fp32"], batch_id="b"))
        for i in range(n):
            scheduler.submit_stage(
                StageRequest.branch(
                    f"reader-{i}", "asr", (8, 8), ["fp32"], batch_id="b"
                )
            )
        scheduler.seal_batch("b")
        scheduler.complete("asr")
        self.assertEqual(len(scheduler.active_readers("asr")), n)

        scheduler.submit_stage(
            StageRequest(
                "overwrite",
                (8, 8),
                ["fp32"],
                batch_id="b",
                access=AccessMode.WRITE,
                source_id="asr",
            )
        )
        self.assertEqual(scheduler.get_request("overwrite").state, RequestState.PENDING)

        barrier = threading.Barrier(n)
        errors: list[BaseException] = []
        threads: list[threading.Thread] = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                scheduler.complete(f"reader-{index}")
            except BaseException as exc:  # captured and asserted in main thread
                errors.append(exc)

        for i in range(n):
            thread = threading.Thread(target=worker, args=(i,))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "reader completion thread timed out")

        self.assertEqual(errors, [])
        self.assertEqual(scheduler.active_readers("asr"), ())
        self.assertEqual(scheduler._buffers["asr"].writer_id, "overwrite")
        self.assertEqual(
            scheduler.get_request("overwrite").state, RequestState.RUNNING
        )
        # Interval allocator remains internally consistent after serialized events.
        snapshot = scheduler.memory_snapshot()
        self.assertGreaterEqual(snapshot["free_bytes"], 0)
        self.assertEqual(
            snapshot["used_bytes"] + snapshot["free_bytes"], snapshot["capacity"]
        )


if __name__ == "__main__":
    unittest.main()
