"""Records are re-verified from their evidence, not trusted as bookkeeping."""

import unittest

from backup_guard import (
    BackupReadGuard,
    EventOrderError,
    FileCategory,
    LockEvent,
    PowerEvent,
    ReadRecord,
    ReadStatus,
    ShardError,
    StaleBootError,
    VerdictCode,
    verify_record,
)


def make_record(**overrides):
    base = dict(
        shard_id="s",
        category=FileCategory.CREDENTIAL_ENCRYPTED,
        boot_id="b1",
        start_seq=1,
        end_seq=2,
        status=ReadStatus.COMPLETED,
        state_at_start={
            "boot_id": "b1",
            "booted": True,
            "unlocked": True,
            "first_unlock_done": True,
        },
    )
    base.update(overrides)
    return ReadRecord(**base)


class VerifyRecordTests(unittest.TestCase):
    def test_valid_credential_record(self):
        v = verify_record(make_record(), "b1")
        self.assertTrue(v.packable)
        self.assertEqual(v.code, VerdictCode.OK)

    def test_forged_first_unlock_flag_rejected(self):
        rec = make_record(state_at_start={"booted": True, "boot_id": "b1",
                                          "unlocked": True,
                                          "first_unlock_done": False})
        v = verify_record(rec, "b1")
        self.assertFalse(v.packable)
        self.assertEqual(v.code, VerdictCode.MISSING_PERMISSION)

    def test_boot_id_mismatch_between_record_and_evidence(self):
        rec = make_record(boot_id="b2")
        v = verify_record(rec, "b1")
        self.assertEqual(v.code, VerdictCode.STALE_BOOT)
        self.assertFalse(v.packable)

    def test_open_record_rejected(self):
        v = verify_record(make_record(status=ReadStatus.OPEN), "b1")
        self.assertEqual(v.code, VerdictCode.STILL_OPEN)

    def test_withdrawn_record_keeps_reason(self):
        rec = make_record(status=ReadStatus.WITHDRAWN,
                          withdrawn_reason="device locked mid-read")
        v = verify_record(rec, "b1")
        self.assertEqual(v.code, VerdictCode.WITHDRAWN)
        self.assertIn("locked", v.detail)
        self.assertEqual(v.evidence["withdrawn_reason"], "device locked mid-read")

    def test_strict_started_locked_rejected(self):
        rec = make_record(
            category=FileCategory.CREDENTIAL_ENCRYPTED_STRICT,
            state_at_start={"boot_id": "b1", "booted": True, "unlocked": False,
                            "first_unlock_done": True},
        )
        v = verify_record(rec, "b1")
        self.assertEqual(v.code, VerdictCode.MISSING_PERMISSION)

    def test_strict_lock_gap_rejected(self):
        rec = make_record(category=FileCategory.CREDENTIAL_ENCRYPTED_STRICT,
                          locked_during_read=True)
        v = verify_record(rec, "b1")
        self.assertEqual(v.code, VerdictCode.UNLOCK_GAP)

    def test_strict_completed_uninterrupted_ok(self):
        rec = make_record(category=FileCategory.CREDENTIAL_ENCRYPTED_STRICT)
        self.assertTrue(verify_record(rec, "b1").packable)

    def test_credential_with_lock_gap_still_ok(self):
        rec = make_record(locked_during_read=True)
        v = verify_record(rec, "b1")
        self.assertTrue(v.packable)
        self.assertIn("continued", v.detail)

    def test_device_encrypted_valid_even_after_new_boot(self):
        rec = make_record(
            category=FileCategory.DEVICE_ENCRYPTED,
            state_at_start={"boot_id": "b1", "booted": True, "unlocked": False,
                            "first_unlock_done": False},
        )
        self.assertTrue(verify_record(rec, "b2").packable)

    def test_not_booted_evidence_rejected(self):
        rec = make_record(state_at_start={"boot_id": None, "booted": False,
                                          "unlocked": False,
                                          "first_unlock_done": False})
        v = verify_record(rec, "b1")
        self.assertEqual(v.code, VerdictCode.MISSING_PERMISSION)

    def test_empty_state_not_silently_accepted(self):
        rec = make_record(state_at_start={})
        self.assertFalse(verify_record(rec, "b1").packable)

    def test_unknown_category_string_rejected(self):
        rec = make_record(category="totally_unknown")
        v = verify_record(rec, "b1")
        self.assertFalse(v.packable)
        self.assertIsNone(v.category)
        self.assertEqual(v.code, VerdictCode.MISSING_PERMISSION)

    def test_category_as_plain_string_still_verifies(self):
        rec = make_record(category="credential_encrypted")
        self.assertTrue(verify_record(rec, "b1").packable)

    def test_unknown_status_rejected(self):
        rec = make_record(status="half_done")
        v = verify_record(rec, "b1")
        self.assertFalse(v.packable)
        self.assertEqual(v.code, VerdictCode.MISSING_PERMISSION)

    def test_previous_boot_accepted_only_when_pinned_check_disabled(self):
        rec = make_record(locked_during_read=True)
        self.assertEqual(verify_record(rec, "b2").code, VerdictCode.STALE_BOOT)
        relaxed = verify_record(rec, "b2", require_current_boot=False)
        self.assertTrue(relaxed.packable)
        # Even relaxed, the record's own evidence must be consistent.
        forged = make_record(
            locked_during_read=True,
            state_at_start={"boot_id": "b1", "booted": True, "unlocked": True,
                            "first_unlock_done": False},
        )
        self.assertFalse(
            verify_record(forged, "b2", require_current_boot=False).packable
        )


class GuardEventValidationTests(unittest.TestCase):
    def setUp(self):
        self.g = BackupReadGuard()

    def test_out_of_order_events_rejected(self):
        self.g.feed(PowerEvent.boot("b1", 5))
        with self.assertRaises(EventOrderError):
            self.g.feed(LockEvent.unlock("b1", 5))
        with self.assertRaises(EventOrderError):
            self.g.feed(LockEvent.unlock("b1", 4))

    def test_lock_event_for_other_boot_rejected(self):
        self.g.feed(PowerEvent.boot("b1", 0))
        with self.assertRaises(StaleBootError):
            self.g.feed(LockEvent.unlock("b2", 1))

    def test_double_boot_same_id_rejected(self):
        self.g.feed(PowerEvent.boot("b1", 0))
        with self.assertRaises(EventOrderError):
            self.g.feed(PowerEvent.boot("b1", 1))

    def test_event_while_off_rejected(self):
        with self.assertRaises(EventOrderError):
            self.g.feed(LockEvent.unlock("b1", 0))

    def test_duplicate_open_read_rejected(self):
        self.g.feed(PowerEvent.boot("b1", 0))
        self.g.start_read("s", FileCategory.DEVICE_ENCRYPTED)
        with self.assertRaises(ShardError):
            self.g.start_read("s", FileCategory.DEVICE_ENCRYPTED)

    def test_abort_read_marks_withdrawn(self):
        self.g.feed(PowerEvent.boot("b1", 0))
        rec = self.g.start_read("s", FileCategory.DEVICE_ENCRYPTED)
        aborted = self.g.abort_read("s", "caller cancelled")
        self.assertIs(rec, aborted)
        self.assertEqual(aborted.status, ReadStatus.WITHDRAWN)
        self.assertFalse(self.g.verdict("s").packable)

    def test_reboot_without_shutdown_still_retires_old_boot(self):
        self.g.feed(PowerEvent.boot("b1", 0))
        self.g.feed(LockEvent.unlock("b1", 1))
        self.g.start_read("s", FileCategory.CREDENTIAL_ENCRYPTED)
        self.g.finish_read("s")
        self.g.feed(PowerEvent.boot("b2", 2))  # direct reboot, no shutdown event
        self.assertEqual(self.g.verdict("s").code, VerdictCode.STALE_BOOT)
        with self.assertRaises(StaleBootError):
            self.g.feed(PowerEvent.boot("b1", 3))
        with self.assertRaises(StaleBootError):
            self.g.feed(LockEvent.unlock("b1", 4))

    def test_feed_all_accepts_list_or_single(self):
        self.g.feed_all([
            PowerEvent.boot("b1", 0),
            LockEvent.unlock("b1", 1),
        ])
        self.assertTrue(self.g.state.first_unlock_done)
        self.g.feed_all(LockEvent.lock("b1", 2))
        self.assertFalse(self.g.state.unlocked)


class PackReportTests(unittest.TestCase):
    def test_report_explains_accepts_and_rejects(self):
        g = BackupReadGuard()
        g.feed_all([PowerEvent.boot("b1", 0), LockEvent.unlock("b1", 1)])
        g.start_read("a", FileCategory.CREDENTIAL_ENCRYPTED)
        g.start_read("b", FileCategory.CREDENTIAL_ENCRYPTED_STRICT)
        g.feed(LockEvent.lock("b1", 2))
        g.finish_read("a")

        report = g.pack_check()
        self.assertFalse(report.packable)
        self.assertEqual([v.shard_id for v in report.accepted], ["a"])
        self.assertEqual([v.shard_id for v in report.rejected], ["b"])
        text = "\n".join(report.explain())
        self.assertIn("SKIP b", text)
        self.assertIn("OK  a", text)
        # Only the soft shard packs, so a partial archive must be explicit.
        g2 = BackupReadGuard()
        g2.feed_all([PowerEvent.boot("b1", 0), LockEvent.unlock("b1", 1)])
        g2.start_read("a", FileCategory.CREDENTIAL_ENCRYPTED)
        g2.finish_read("a")
        self.assertTrue(g2.pack_check().packable)

    def test_verdict_of_unknown_shard(self):
        g = BackupReadGuard()
        self.assertEqual(g.verdict("ghost").code, VerdictCode.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
