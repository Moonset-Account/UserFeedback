from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from buffer_scheduler import (  # noqa: E402
    BufferScheduler,
    MemoryPressureError,
    ModelLayout,
    ReleaseReason,
    RequestState,
    Shape,
    StageRequest,
)


class LayoutAndAllocationTests(unittest.TestCase):
    def test_shape_strides_and_format_negotiation(self):
        layout = ModelLayout("m", alignment=128)
        binding = layout.bind(Shape((2, 3)), ["float16", "fp32"])
        self.assertEqual(binding.format, "fp16")
        self.assertEqual(binding.itemsize, 2)
        self.assertEqual(binding.strides, (3, 1))
        self.assertEqual(binding.logical_bytes, 12)
        self.assertEqual(binding.aligned_size, 128)
        self.assertEqual(binding.model_version, "m")

        smallest = BufferScheduler(
            model=ModelLayout("m", alignment=1), format_policy="smallest"
        )
        req = smallest.submit_stage(
            StageRequest("t", (4, 4), ["fp32", "int8"], batch_id="b")
        )
        self.assertEqual(req.request.selected_format, "int8")
        self.assertEqual(req.region.size, 16)

    def test_aligned_regions_are_placed_in_pool(self):
        scheduler = BufferScheduler(4096, model=ModelLayout("m", alignment=64))
        first = scheduler.submit_stage(StageRequest("a", (1, 3), ["fp32"]))
        second = scheduler.submit_stage(StageRequest("b", (1, 3), ["fp32"]))

        self.assertEqual((first.region.offset, first.region.size), (0, 64))
        # 12 logical bytes round to 64; next 64-byte block starts at 64.
        self.assertEqual((second.region.offset, second.region.size), (64, 64))
        self.assertTrue(scheduler.pool.can_allocate(64, 64))

    def test_evictable_idle_buffer_is_reclaimed_under_pressure(self):
        scheduler = BufferScheduler(512, model=ModelLayout("m", alignment=1))
        cache = scheduler.submit_stage(
            StageRequest("cache", (128,), ["int8"], evictable=True)
        )
        scheduler.complete("cache")
        pinned = scheduler.submit_stage(StageRequest("pinned", (384,), ["int8"]))
        self.assertEqual(pinned.region.offset, 128)

        needed = scheduler.submit_stage(StageRequest("needed", (128,), ["int8"]))
        reasons = needed.release_reasons
        self.assertIn(ReleaseReason.MEMORY_PRESSURE_EVICTION.value, reasons)
        eviction = next(
            r for r in needed.releases if r.reason == ReleaseReason.MEMORY_PRESSURE_EVICTION.value
        )
        self.assertEqual(eviction.region.region_id, cache.region.region_id)
        self.assertEqual(needed.state, RequestState.RUNNING)
        self.assertEqual(needed.region.offset, 0)
        self.assertEqual(scheduler.memory_snapshot()["free_bytes"], 0)

    def test_fragmented_pool_relocates_idle_movable_buffer(self):
        scheduler = BufferScheduler(512, model=ModelLayout("m", alignment=1))
        scheduler.submit_stage(StageRequest("x", (128,), ["int8"]))
        middle = scheduler.submit_stage(
            StageRequest("middle", (128,), ["int8"], evictable=False, movable=True)
        )
        scheduler.submit_stage(StageRequest("tail", (256,), ["int8"]))
        scheduler.write_payload("middle", {"synthetic_samples": [1, 2, 3]})

        # Completion releases write leases while retaining the idle buffers.
        scheduler.complete("x")
        scheduler.complete("middle")
        scheduler.complete("tail")
        scheduler.release_buffer("x")
        scheduler.release_buffer("tail")
        self.assertEqual(scheduler.pool.largest_free_block(), 256)

        big = scheduler.submit_stage(StageRequest("big", (384,), ["int8"]))
        realloc_steps = [s for s in big.steps if s.action == "region_reallocated"]
        self.assertEqual(len(realloc_steps), 1)
        realloc_step = realloc_steps[0]
        self.assertEqual(
            realloc_step.release_reason,
            ReleaseReason.MEMORY_PRESSURE_RELOCATION.value,
        )
        self.assertEqual(realloc_step.release.region.offset, 128)
        self.assertEqual(realloc_step.region.offset, 0)
        self.assertEqual(big.region.offset, 128)
        self.assertEqual(big.region.size, 384)
        self.assertEqual(
            scheduler.read_payload("middle"), {"synthetic_samples": [1, 2, 3]}
        )
        self.assertIs(
            scheduler.read_payload("middle"),
            scheduler._buffers["middle"].region.payload,
        )
        self.assertEqual(scheduler.current_region("middle").offset, 0)

    def test_impossible_pressure_is_reported_without_losing_request(self):
        scheduler = BufferScheduler(128, model=ModelLayout("m", alignment=1))
        scheduler.submit_batch("full-batch", [StageRequest("p", (128,), ["int8"])])
        blocked = scheduler.submit_stage(StageRequest("q", (1,), ["int8"], batch_id="q-batch"))
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.state, RequestState.PENDING)
        self.assertEqual(blocked.request.blocked_reason, "memory_pressure")
        self.assertIsInstance(blocked.error, MemoryPressureError)

        with self.assertRaises(MemoryPressureError):
            scheduler.submit_stage(
                StageRequest("oversize", (129,), ["int8"]), strict=True
            )


if __name__ == "__main__":
    unittest.main()
