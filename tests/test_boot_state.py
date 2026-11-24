import unittest

from backup_guard import (
    BackupReadGuard,
    DeviceLockedError,
    EventKind,
    FileCategory,
    FirstUnlockRequiredError,
    LockEvent,
    PowerEvent,
    ReadStatus,
    ShardError,
    VerdictCode,
)
from backup_guard.state import NotBootedError


class BootStateTests(unittest.TestCase):
    def setUp(self):
        self.g = BackupReadGuard()

    def boot(self, boot_id="b1", seq=0):
        self.g.feed(PowerEvent.boot(boot_id, seq))

    def test_device_encrypted_readable_before_any_unlock(self):
        self.boot()
        self.assertFalse(self.g.state.unlocked)
        self.assertFalse(self.g.state.first_unlock_done)
        rec = self.g.start_read("s1", FileCategory.DEVICE_ENCRYPTED)
        self.assertEqual(rec.status, ReadStatus.OPEN)
        self.assertEqual(self.g.verdict("s1").code, VerdictCode.STILL_OPEN)
        self.g.finish_read("s1")
        self.assertTrue(self.g.verdict("s1").packable)

    def test_credential_requires_first_unlock(self):
        self.boot()
        with self.assertRaises(FirstUnlockRequiredError):
            self.g.start_read("s1", FileCategory.CREDENTIAL_ENCRYPTED)
        self.assertEqual(self.g.records("s1"), ())

    def test_credential_readable_after_first_unlock(self):
        self.boot()
        self.g.feed(LockEvent.unlock("b1", 1))
        self.assertTrue(self.g.state.first_unlock_done)
        rec = self.g.start_read("s1", FileCategory.CREDENTIAL_ENCRYPTED)
        self.assertTrue(rec.state_at_start["first_unlock_done"])

    def test_strict_requires_unlocked_now(self):
        self.boot()
        self.g.feed(LockEvent.unlock("b1", 1))
        self.g.feed(LockEvent.lock("b1", 2))
        self.assertTrue(self.g.state.first_unlock_done)
        with self.assertRaises(DeviceLockedError):
            self.g.start_read("strict", FileCategory.CREDENTIAL_ENCRYPTED_STRICT)

    def test_read_before_boot_rejected(self):
        with self.assertRaises(NotBootedError):
            self.g.start_read("s1", FileCategory.DEVICE_ENCRYPTED)

    def test_device_encrypted_readable_while_locked(self):
        self.boot()
        self.g.start_read("s1", FileCategory.DEVICE_ENCRYPTED)
        self.g.feed(LockEvent.lock("b1", 1))
        # Still able to open another device-encrypted shard while locked.
        self.g.start_read("s2", FileCategory.DEVICE_ENCRYPTED)
        self.assertTrue(self.g.state.readable(FileCategory.DEVICE_ENCRYPTED))

    def test_unknown_event_kind_rejected(self):
        with self.assertRaises(ValueError):
            PowerEvent(EventKind.UNLOCK, "b1", 0)
        with self.assertRaises(ValueError):
            LockEvent(EventKind.BOOT, "b1", 0)


class LockMidReadTests(unittest.TestCase):
    def setUp(self):
        self.g = BackupReadGuard()
        self.g.feed(PowerEvent.boot("b1", 0))
        self.g.feed(LockEvent.unlock("b1", 1))

    def test_credential_survives_lock_strict_withdrawn(self):
        soft = self.g.start_read("soft", FileCategory.CREDENTIAL_ENCRYPTED)
        hard = self.g.start_read("hard", FileCategory.CREDENTIAL_ENCRYPTED_STRICT)

        self.g.feed(LockEvent.lock("b1", 2))

        self.assertIs(self.g.open_reads[0], soft)
        self.assertTrue(soft.locked_during_read)
        self.assertEqual(hard.status, ReadStatus.WITHDRAWN)
        self.assertIn("locked", hard.withdrawn_reason or "")
        self.assertNotIn("hard", [r.shard_id for r in self.g.open_reads])

        self.g.finish_read("soft")

        report = self.g.pack_check()
        verdicts = report.by_shard()
        self.assertTrue(verdicts["soft"].packable)
        self.assertEqual(verdicts["soft"].code, VerdictCode.OK)
        self.assertFalse(verdicts["hard"].packable)
        self.assertEqual(verdicts["hard"].code, VerdictCode.WITHDRAWN)

    def test_strict_retry_after_reunlock_completes(self):
        self.g.start_read("hard", FileCategory.CREDENTIAL_ENCRYPTED_STRICT)
        self.g.feed(LockEvent.lock("b1", 2))
        self.g.feed(LockEvent.unlock("b1", 3))
        again = self.g.start_read("hard", FileCategory.CREDENTIAL_ENCRYPTED_STRICT)
        self.assertEqual(again.attempt, 2)
        self.g.finish_read("hard")
        v = self.g.verdict("hard")
        self.assertTrue(v.packable)
        self.assertEqual(v.code, VerdictCode.OK)
        self.assertEqual(v.attempt, 2)

    def test_lock_after_strict_completion_is_harmless(self):
        self.g.start_read("hard", FileCategory.CREDENTIAL_ENCRYPTED_STRICT)
        self.g.finish_read("hard")
        self.g.feed(LockEvent.lock("b1", 2))
        self.assertTrue(self.g.verdict("hard").packable)

    def test_reboot_withdraws_open_strict_and_credential(self):
        self.g.start_read("soft", FileCategory.CREDENTIAL_ENCRYPTED)
        self.g.start_read("hard", FileCategory.CREDENTIAL_ENCRYPTED_STRICT)
        self.g.feed(PowerEvent.boot("b2", 2))
        self.assertEqual(self.g.open_reads, ())
        self.assertEqual(self.g.records("soft")[0].status, ReadStatus.WITHDRAWN)
        self.assertEqual(self.g.records("hard")[0].status, ReadStatus.WITHDRAWN)

    def test_shutdown_withdraws_open_reads(self):
        self.g.start_read("soft", FileCategory.CREDENTIAL_ENCRYPTED)
        self.g.feed(PowerEvent.shutdown("b1", 2))
        self.assertEqual(self.g.records("soft")[0].status, ReadStatus.WITHDRAWN)
        self.assertFalse(self.g.state.booted)

    def test_finish_after_reboot_is_withdrawn_not_completed(self):
        self.g.start_read("soft", FileCategory.CREDENTIAL_ENCRYPTED)
        self.g.feed(PowerEvent.boot("b2", 2))
        with self.assertRaises(ShardError):
            self.g.finish_read("soft")


class RebootFirstUnlockTests(unittest.TestCase):
    def setUp(self):
        self.g = BackupReadGuard()
        self.g.feed(PowerEvent.boot("b1", 0))
        self.g.feed(LockEvent.unlock("b1", 1))
        self.g.start_read("soft", FileCategory.CREDENTIAL_ENCRYPTED)
        self.g.finish_read("soft")
        self.g.start_read("dev", FileCategory.DEVICE_ENCRYPTED)
        self.g.finish_read("dev")

    def test_first_unlock_must_be_reconfirmed(self):
        self.g.feed(PowerEvent.shutdown("b1", 2))
        self.g.feed(PowerEvent.boot("b2", 3))
        self.assertFalse(self.g.state.first_unlock_done)
        with self.assertRaises(FirstUnlockRequiredError):
            self.g.start_read("soft2", FileCategory.CREDENTIAL_ENCRYPTED)

        # Previous-boot credential shard no longer packable.
        v = self.g.verdict("soft")
        self.assertFalse(v.packable)
        self.assertEqual(v.code, VerdictCode.STALE_BOOT)

        # Device-encrypted shard stays valid across boots.
        self.assertTrue(self.g.verdict("dev").packable)

        self.g.feed(LockEvent.unlock("b2", 4))
        self.g.start_read("soft2", FileCategory.CREDENTIAL_ENCRYPTED)
        self.g.finish_read("soft2")
        self.assertTrue(self.g.verdict("soft2").packable)

    def test_pack_reports_missing_expected_shard(self):
        report = self.g.pack_check(expected=["soft", "nope"])
        self.assertFalse(report.packable)
        self.assertEqual(report.missing, ("nope",))
        self.assertEqual(report.by_shard()["nope"].code, VerdictCode.NOT_FOUND)

    def test_reboot_requires_new_boot_id(self):
        from backup_guard import StaleBootError

        self.g.feed(PowerEvent.shutdown("b1", 2))
        self.g.feed(PowerEvent.boot("b2", 3))
        self.g.feed(PowerEvent.shutdown("b2", 4))
        with self.assertRaises(StaleBootError):
            self.g.feed(PowerEvent.boot("b1", 5))


if __name__ == "__main__":
    unittest.main()
