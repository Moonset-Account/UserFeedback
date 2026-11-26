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
)


class SyntheticSpeechPipelineTests(unittest.TestCase):
    def test_asr_output_fans_out_to_decode_and_second_model(self):
        # Local deterministic synthetic log-mel-like data; no network or files.
        frames, bins = 32, 16
        synthetic_mel = [
            [((frame * 17 + bin_index * 31) % 13) / 13.0 for bin_index in range(bins)]
            for frame in range(frames)
        ]
        gpu_output = {"mel": synthetic_mel, "token_logits": [0.1, 0.7, 0.2]}
        producer_receipts = []

        v1 = ModelLayout("asr-phase1", alignment=128, layout_name="dense-v1")
        v2 = ModelLayout("asr-phase2-hot", alignment=256, layout_name="dense-v2")
        scheduler = BufferScheduler(1 << 20, model=v1)

        submitted = scheduler.submit_output(
            "gpu-output",
            (1, frames, bins),
            ["fp16", "fp32"],
            dependent_stages=["ctc-decode", "second-model"],
            on_complete=producer_receipts.append,
            receipt_id="gpu-output-receipt",
        )
        self.assertTrue(submitted.ok)
        self.assertEqual(
            submitted.data["dependent_stages"],
            ["gpu-output:ctc-decode", "gpu-output:second-model"],
        )
        producer_region = scheduler.current_region("gpu-output")
        self.assertEqual(producer_region.format, "fp16")
        self.assertEqual(producer_region.model_version, "asr-phase1")
        self.assertEqual(producer_region.size, 1024)  # 512 values * 2 bytes

        completed = scheduler.complete("gpu-output", payload=gpu_output)
        self.assertEqual(completed.receipt.receipt_id, "gpu-output-receipt")
        self.assertEqual(len(producer_receipts), 1)
        self.assertIs(producer_receipts[0], completed.receipt)

        decode_id = "gpu-output:ctc-decode"
        second_id = "gpu-output:second-model"
        decode = scheduler.get_request(decode_id)
        second = scheduler.get_request(second_id)
        self.assertEqual(decode.state, RequestState.RUNNING)
        self.assertEqual(second.state, RequestState.RUNNING)
        self.assertEqual(decode.region.region_id, producer_region.region_id)
        self.assertEqual(second.region.region_id, producer_region.region_id)
        self.assertIs(scheduler.read_payload(decode_id), gpu_output)
        self.assertIs(scheduler.read_payload(second_id), gpu_output)

        # Hot update between branch completions cannot change the pinned region.
        scheduler.hot_swap_model(v2)
        decode_done = scheduler.complete(decode_id)
        self.assertEqual(decode_done.receipt.model_version, "asr-phase1")
        self.assertEqual(scheduler.active_readers("gpu-output"), (second_id,))
        self.assertEqual(scheduler.current_region("gpu-output").alignment, 128)

        second_done = scheduler.complete(second_id)
        self.assertEqual(second_done.receipt.model_version, "asr-phase1")
        self.assertIn(
            ReleaseReason.BATCH_COMPLETED.value, second_done.release_reasons
        )
        self.assertIsNone(scheduler.current_region("gpu-output"))

        new_batch = scheduler.submit_output(
            "post-hot-output",
            (1, frames, bins),
            ["fp16", "fp32"],
            dependent_stages=["second-model"],
        )
        new_region = scheduler.current_region("post-hot-output")
        self.assertEqual(new_region.model_version, "asr-phase2-hot")
        self.assertEqual(new_region.alignment, 256)
        self.assertEqual(new_region.size, 1024)  # logical 1024 already aligns to 256
        self.assertTrue(new_batch.ok)


if __name__ == "__main__":
    unittest.main()
