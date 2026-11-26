from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from buffer_scheduler import (  # noqa: E402
    BufferScheduler,
    ModelLayout,
    ReleaseReason,
    RequestState,
    StageRequest,
)


class CancellationAndReallocationTests(unittest.TestCase):
    def test_cancelled_producer_memory_is_reallocated_to_waiting_request(self):
        # Use byte-sized int8 allocations and alignment 1 for exact reasoning.
        from buffer_scheduler import ModelLayout

        scheduler = BufferScheduler(128, model=ModelLayout("byte-layout", alignment=1))
        scheduler.submit_batch(
            "b",
            [StageRequest("producer", (128,), ["int8"])],
        )
        waiting = scheduler.submit_stage(
            StageRequest("replacement", (64,), ["int8"], batch_id="b")
        )
        self.assertEqual(waiting.state, RequestState.PENDING)
        self.assertEqual(waiting.request.blocked_reason, "memory_pressure")

        cancelled = scheduler.cancel_branch("producer")
        self.assertEqual(cancelled.state, RequestState.CANCELLED)
        self.assertIn(
            ReleaseReason.REQUEST_CANCELLED.value,
            cancelled.release_reasons,
        )
        replacement = scheduler.get_request("replacement")
        self.assertEqual(replacement.state, RequestState.RUNNING)
        self.assertEqual(replacement.region.offset, 0)
        self.assertEqual(replacement.region.size, 64)
        self.assertEqual(scheduler.memory_snapshot()["used_bytes"], 64)

    def test_pressure_relocation_waits_for_active_reader(self):
        scheduler = BufferScheduler(512, model=ModelLayout("byte", alignment=1))
        scheduler.submit_stage(StageRequest("x", (128,), ["int8"]))
        scheduler.submit_stage(StageRequest("middle", (128,), ["int8"]))
        scheduler.submit_stage(StageRequest("tail", (256,), ["int8"]))
        for request_id in ("x", "middle", "tail"):
            scheduler.complete(request_id)
        scheduler.release_buffer("x")
        scheduler.release_buffer("tail")

        scheduler.submit_stage(
            StageRequest.branch("middle-reader", "middle", (128,), ["int8"])
        )
        blocked = scheduler.submit_stage(StageRequest("big", (384,), ["int8"]))
        self.assertEqual(blocked.state, RequestState.PENDING)
        self.assertEqual(blocked.request.blocked_reason, "memory_pressure")
        self.assertEqual(scheduler.current_region("middle").offset, 128)

        finished = scheduler.complete("middle-reader")
        actions = [step.action for step in finished.steps]
        self.assertIn("region_reallocated", actions)
        self.assertIn("region_allocated", actions)
        self.assertEqual(scheduler.get_request("big").state, RequestState.RUNNING)
        self.assertEqual(scheduler.current_region("middle").offset, 0)
        self.assertEqual(scheduler.current_region("big").offset, 128)

    def test_reclaim_refuses_active_leases(self):
        scheduler = BufferScheduler(64, model=ModelLayout("byte-layout", alignment=1))
        scheduler.submit_stage(
            StageRequest("p", (64,), ["int8"], evictable=True)
        )
        scheduler.submit_stage(
            StageRequest.branch("r1", "p", (64,), ["int8"])
        )
        scheduler.complete("p")
        self.assertEqual(scheduler.active_readers("p"), ("r1",))

        result = scheduler.reclaim_memory(needed_bytes=1)
        self.assertEqual(result.data["evicted_count"], 0)
        self.assertIsNotNone(scheduler.current_region("p"))

        scheduler.cancel_branch("r1")
        # The unsealed default batch retains p after the reader cancellation;
        # explicit reclaim can now evict the idle, evictable buffer.
        reclaimed = scheduler.reclaim_memory(needed_bytes=1)
        self.assertEqual(reclaimed.data["evicted_count"], 1)
        self.assertIsNone(scheduler.current_region("p"))


if __name__ == "__main__":
    unittest.main()
