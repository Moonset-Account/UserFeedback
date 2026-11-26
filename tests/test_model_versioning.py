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


class ModelVersioningTests(unittest.TestCase):
    def test_hot_swap_pins_in_flight_batch_to_old_layout(self):
        v1 = ModelLayout("asr-v1", alignment=64, layout_name="row64")
        v2 = ModelLayout("asr-v2", alignment=128, layout_name="row128")
        scheduler = BufferScheduler(4096, model=v1)

        old = scheduler.submit_batch(
            "old-batch",
            [
                StageRequest("old-producer", (1, 3), ["fp32"]),
                StageRequest.branch(
                    "old-branch", "old-producer", (1, 3), ["fp32"]
                ),
            ],
        )
        self.assertTrue(old.ok)
        producer_info = scheduler.get_request("old-producer")
        self.assertEqual(producer_info.region.model_version, "asr-v1")
        self.assertEqual(producer_info.region.alignment, 64)
        self.assertEqual(producer_info.region.layout_key, v1.bind((1, 3), ["fp32"]).layout_key)

        swap = scheduler.hot_swap_model(v2)
        self.assertEqual(swap.data["previous_version"], "asr-v1")
        self.assertEqual(scheduler.active_model_version, "asr-v2")

        # A stage added to the already opened batch still uses v1 geometry.
        extra_old = scheduler.submit_stage(
            StageRequest("old-extra", (1, 3), ["fp32"], batch_id="old-batch")
        )
        self.assertEqual(extra_old.region.model_version, "asr-v1")
        self.assertEqual(extra_old.region.offset, 64)
        self.assertEqual(extra_old.region.alignment, 64)

        # Completion and the zero-copy branch receipt retain the old version.
        completed = scheduler.complete("old-producer")
        branch_info = scheduler.get_request("old-branch")
        self.assertEqual(branch_info.state, RequestState.RUNNING)
        self.assertEqual(branch_info.region.model_version, "asr-v1")
        self.assertEqual(branch_info.region.region_id, producer_info.region.region_id)
        self.assertEqual(completed.receipt.model_version, "asr-v1")

        # A new batch after the swap uses v2 placement/layout.
        new_batch = scheduler.submit_batch(
            "new-batch",
            [StageRequest("new-producer", (1, 3), ["fp32"])],
        )
        new_info = scheduler.get_request("new-producer")
        self.assertEqual(new_info.region.model_version, "asr-v2")
        self.assertEqual(new_info.region.alignment, 128)
        self.assertIn("row128", new_info.region.layout_key)
        # Old allocations occupy [0,128): old producer 0..64 and old-extra 64..128.
        # v2 alignment moves the new region to offset 128 with size 128.
        self.assertEqual((new_info.region.offset, new_info.region.size), (128, 128))

        scheduler.complete("old-branch")
        # old-extra is still running, so old layout memory is not released yet.
        self.assertIsNotNone(scheduler.current_region("old-producer"))
        final_old = scheduler.complete("old-extra")
        old_batch = scheduler.batch_snapshot("old-batch")
        self.assertTrue(old_batch["completed"])
        self.assertTrue(old_batch["released"])
        region_releases = [r for r in final_old.releases if r.release_kind == "region"]
        self.assertEqual(len(region_releases), 2)
        self.assertTrue(
            all(r.reason == ReleaseReason.BATCH_COMPLETED.value for r in region_releases)
        )
        self.assertTrue(all(r.region.model_version == "asr-v1" for r in region_releases))

    def test_explicit_model_version_and_re_registration_conflict(self):
        v1 = ModelLayout("v1", alignment=64)
        v2 = ModelLayout("v2", alignment=256)
        scheduler = BufferScheduler(models=[v1, v2], active_version="v1")
        result = scheduler.submit_batch(
            "pinned-v2",
            [StageRequest("p", (1,), ["int8"])],
            model_version="v2",
        )
        self.assertTrue(result.ok)
        info = scheduler.get_request("p")
        self.assertEqual(info.model_version, "v2")
        self.assertEqual(info.region.alignment, 256)

        with self.assertRaises(Exception):
            scheduler.register_model(ModelLayout("v2", alignment=128))


if __name__ == "__main__":
    unittest.main()
