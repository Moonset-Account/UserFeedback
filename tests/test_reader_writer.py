from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from buffer_scheduler import (  # noqa: E402
    AccessMode,
    BufferScheduler,
    ReleaseReason,
    RequestState,
    StageRequest,
)


class ReaderWriterTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = BufferScheduler(4096)
        self.shape = (2, 2)
        self.formats = ["fp32"]

    def _producer_and_branches(self):
        s = self.scheduler
        s.submit_stage(
            StageRequest(
                "asr",
                self.shape,
                self.formats,
                batch_id="b1",
                stage_name="asr",
            )
        )
        s.submit_stage(
            StageRequest.branch("branch-a", "asr", self.shape, self.formats, batch_id="b1")
        )
        s.submit_stage(
            StageRequest.branch("branch-b", "asr", self.shape, self.formats, batch_id="b1")
        )
        s.seal_batch("b1")
        return s.complete("asr", payload={"pcm": [0.1, 0.2], "embedding": object()})

    def test_multiple_branches_share_same_zero_copy_region(self):
        self._producer_and_branches()
        a = self.scheduler.get_request("branch-a")
        b = self.scheduler.get_request("branch-b")
        self.assertEqual(a.state, RequestState.RUNNING)
        self.assertEqual(b.state, RequestState.RUNNING)
        self.assertIsNotNone(a.region)
        self.assertEqual(a.region.region_id, b.region.region_id)
        self.assertEqual(a.region.offset, b.region.offset)
        self.assertEqual(a.region.size, b.region.size)
        self.assertEqual(self.scheduler.active_readers("asr"), ("branch-a", "branch-b"))

        payload = self.scheduler.read_payload("asr")
        self.assertEqual(payload["pcm"], [0.1, 0.2])
        # The payload object remains the same instance: no simulated copy on share.
        self.assertIs(payload, self.scheduler.read_payload("branch-a"))

    def test_overwrite_waits_for_all_relevant_readers(self):
        self._producer_and_branches()
        self.scheduler.complete("branch-a")

        overwrite = self.scheduler.submit_stage(
            StageRequest(
                "overwrite",
                self.shape,
                self.formats,
                batch_id="b1",
                access=AccessMode.WRITE,
                source_id="asr",
            )
        )
        self.assertEqual(overwrite.state, RequestState.PENDING)
        self.assertEqual(overwrite.request.blocked_reason, "readers_drain")
        self.assertEqual(self.scheduler.waiting_writers("asr"), ("overwrite",))
        self.assertEqual(self.scheduler.active_readers("asr"), ("branch-b",))

        result = self.scheduler.complete("branch-b")
        actions = [step.action for step in result.steps]
        self.assertIn("lease_released", actions)
        self.assertIn("write_granted", actions)
        granted = next(step for step in result.steps if step.action == "write_granted")
        self.assertEqual(granted.region.region_id, overwrite.request.region.region_id)
        self.assertEqual(granted.region.region_id, self.scheduler.current_region("asr").region_id)
        self.assertEqual(self.scheduler.get_request("overwrite").state, RequestState.RUNNING)

        finished = self.scheduler.complete("overwrite")
        self.assertIn(
            ReleaseReason.BATCH_COMPLETED.value,
            finished.release_reasons,
        )
        self.assertIsNone(self.scheduler.current_region("asr"))

    def test_cancel_one_reader_releases_only_that_branch(self):
        self._producer_and_branches()
        self.scheduler.submit_stage(
            StageRequest(
                "overwrite",
                self.shape,
                self.formats,
                batch_id="b1",
                access=AccessMode.WRITE,
                source_id="asr",
            )
        )

        cancelled = self.scheduler.cancel_branch("branch-a")
        self.assertEqual(cancelled.state, RequestState.CANCELLED)
        self.assertIn(
            ReleaseReason.BRANCH_CANCELLED.value,
            cancelled.release_reasons,
        )
        self.assertEqual(
            self.scheduler.get_request("branch-b").state, RequestState.RUNNING
        )
        self.assertEqual(self.scheduler.active_readers("asr"), ("branch-b",))
        self.assertEqual(
            self.scheduler.get_request("overwrite").state, RequestState.PENDING
        )

        last = self.scheduler.cancel_branch("branch-b")
        granted = [s for s in last.steps if s.action == "write_granted"]
        self.assertEqual(len(granted), 1)
        self.assertEqual(
            self.scheduler.get_request("overwrite").state, RequestState.RUNNING
        )

    def test_fair_queue_does_not_let_later_readers_jump_ahead_of_writer(self):
        self._producer_and_branches()
        # branch-a and branch-b are active from the helper. Complete one so the
        # interleaving starts from a single relevant reader.
        self.scheduler.complete("branch-a")

        writer = self.scheduler.submit_stage(
            StageRequest(
                "writer",
                self.shape,
                self.formats,
                batch_id="b1",
                access=AccessMode.WRITE,
                source_id="asr",
            )
        )
        self.assertEqual(writer.state, RequestState.PENDING)

        late_reader = self.scheduler.submit_stage(
            StageRequest.branch(
                "branch-late", "asr", self.shape, self.formats, batch_id="b1"
            )
        )
        self.assertEqual(late_reader.state, RequestState.PENDING)
        self.assertEqual(
            late_reader.request.blocked_reason, "write_pending_fair_queue"
        )

        self.scheduler.complete("branch-b")
        self.assertEqual(self.scheduler.get_request("writer").state, RequestState.RUNNING)
        self.assertEqual(
            self.scheduler.get_request("branch-late").state, RequestState.PENDING
        )

        self.scheduler.complete("writer")
        self.assertEqual(
            self.scheduler.get_request("branch-late").state, RequestState.RUNNING
        )
        self.assertEqual(
            self.scheduler.active_readers("asr"), ("branch-late",)
        )

    def test_cancel_pending_branch_propagates_to_its_dependents(self):
        s = self.scheduler
        s.submit_stage(StageRequest("source", self.shape, self.formats))
        s.submit_stage(
            StageRequest.branch("branch", "source", self.shape, self.formats)
        )
        s.submit_stage(
            StageRequest(
                "downstream",
                self.shape,
                self.formats,
                access=AccessMode.READ,
                source_id="branch",
                depends_on=("branch",),
            )
        )
        result = s.cancel_branch("branch")
        ids = {r.request_id: r.status for r in result.receipts}
        self.assertEqual(ids["branch"], RequestState.CANCELLED)
        self.assertEqual(ids["downstream"], RequestState.CANCELLED)
        self.assertEqual(
            s.get_request("downstream").terminal_reason,
            ReleaseReason.DEPENDENCY_CANCELLED.value,
        )

    def test_completion_receipt_and_callback_are_delivered(self):
        delivered = []
        s = BufferScheduler(4096)
        s.submit_stage(
            StageRequest(
                "asr",
                self.shape,
                self.formats,
                on_complete=delivered.append,
                receipt_id="caller-receipt-1",
            )
        )
        result = s.complete("asr")
        self.assertEqual(result.receipt.receipt_id, "caller-receipt-1")
        self.assertEqual(len(delivered), 1)
        self.assertIs(delivered[0], result.receipt)
        self.assertEqual(delivered[0].status, RequestState.COMPLETED)
        self.assertIsNotNone(delivered[0].region)

        duplicate = s.complete("asr")
        self.assertFalse(duplicate.changed)
        self.assertEqual(len(delivered), 1)

    def test_incompatible_branch_format_fails_with_receipt_after_dependency(self):
        s = self.scheduler
        s.submit_stage(StageRequest("asr", self.shape, ["fp32"], batch_id="fmt"))
        s.submit_stage(
            StageRequest.branch(
                "bad-branch", "asr", self.shape, ["int8"], batch_id="fmt"
            )
        )
        s.seal_batch("fmt")
        result = s.complete("asr")
        failed = [step for step in result.steps if step.action == "request_failed"]
        self.assertEqual(len(failed), 1)
        info = s.get_request("bad-branch")
        self.assertEqual(info.state, RequestState.FAILED)
        self.assertIn("fp32", info.terminal_reason)
        self.assertIn("bad-branch", s.receipts)

    def test_dependency_validation_and_cycle(self):
        s = BufferScheduler()
        s.submit_stage(StageRequest("a", (1,), ["int8"], depends_on=("missing",)))
        with self.assertRaises(Exception):
            s.seal_batch("default")

        cyclic = BufferScheduler()
        with self.assertRaises(Exception):
            cyclic.submit_batch(
                "cyc",
                [
                    StageRequest("x", (1,), ["int8"], depends_on=("y",)),
                    StageRequest("y", (1,), ["int8"], depends_on=("x",)),
                ],
            )

    def test_trace_sequence_is_contiguous(self):
        self._producer_and_branches()
        sequences = [step.sequence for step in self.scheduler.trace]
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))

    def test_region_steps_and_releases_always_carry_actual_region(self):
        self._producer_and_branches()
        self.scheduler.complete("branch-a")
        self.scheduler.cancel_branch("branch-b")

        region_actions = {
            "region_allocated",
            "region_shared",
            "write_started",
            "lease_released",
            "region_released",
            "request_completed",
            "request_cancelled",
        }
        for step in self.scheduler.trace:
            if step.action in region_actions:
                self.assertIsNotNone(step.region, step.action)
                self.assertTrue(step.region.region_id.startswith("R"))
            if step.release is not None:
                self.assertTrue(step.release_reason)
                self.assertIsNotNone(step.release.region)
                self.assertEqual(step.sequence, step.release.sequence)


if __name__ == "__main__":
    unittest.main()
