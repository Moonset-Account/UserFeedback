"""End-to-end timelines mixing power, lock, and shard-read events."""

import unittest

from backup_guard import (
    BackupReadGuard,
    DeviceLockedError,
    FileCategory,
    FirstUnlockRequiredError,
    LockEvent,
    PowerEvent,
    ReadStatus,
    VerdictCode,
)


class FullBackupScenarioTests(unittest.TestCase):
    def test_backup_interleaved_with_lock_and_reboot(self):
        g = BackupReadGuard()

        # Boot; only device-encrypted work is possible until first unlock.
        g.feed(PowerEvent.boot("boot-A", 0))
        g.start_read("dev-1", FileCategory.DEVICE_ENCRYPTED)
        with self.assertRaises(FirstUnlockRequiredError):
            g.start_read("cred-1", FileCategory.CREDENTIAL_ENCRYPTED)

        # First unlock since boot.
        g.feed(LockEvent.unlock("boot-A", 1))
        g.start_read("cred-1", FileCategory.CREDENTIAL_ENCRYPTED)
        g.start_read("strict-1", FileCategory.CREDENTIAL_ENCRYPTED_STRICT)

        # Screen locks mid-read: strict withdrawn, credential keeps going.
        g.feed(LockEvent.lock("boot-A", 2))
        self.assertEqual(
            sorted(r.shard_id for r in g.open_reads), ["cred-1", "dev-1"]
        )
        self.assertEqual(g.records("strict-1")[0].status, ReadStatus.WITHDRAWN)

        # The credential shard finishes while still locked.
        g.finish_read("cred-1")
        self.assertTrue(g.verdict("cred-1").packable)

        # Cannot start a strict read while locked.
        with self.assertRaises(DeviceLockedError):
            g.start_read("strict-2", FileCategory.CREDENTIAL_ENCRYPTED_STRICT)

        # Unlock again and retry the strict shard.
        g.feed(LockEvent.unlock("boot-A", 3))
        g.start_read("strict-1", FileCategory.CREDENTIAL_ENCRYPTED_STRICT)
        self.assertEqual(g.latest_record("strict-1").attempt, 2)
        g.finish_read("strict-1")
        self.assertTrue(g.verdict("strict-1").packable)

        # dev-1 opened before any unlock but is now finished.
        g.finish_read("dev-1")
        self.assertTrue(g.verdict("dev-1").packable)

        report = g.pack_check(expected=["dev-1", "cred-1", "strict-1"])
        self.assertTrue(report.packable)

        # Reboot: first unlock confirmation is gone, credential shards re-read.
        g.feed(PowerEvent.shutdown("boot-A", 4))
        g.feed(PowerEvent.boot("boot-B", 5))
        self.assertFalse(g.state.first_unlock_done)
        self.assertEqual(g.verdict("dev-1").code, VerdictCode.OK)
        self.assertEqual(g.verdict("cred-1").code, VerdictCode.STALE_BOOT)
        self.assertEqual(g.verdict("strict-1").code, VerdictCode.STALE_BOOT)

        with self.assertRaises(FirstUnlockRequiredError):
            g.start_read("cred-2", FileCategory.CREDENTIAL_ENCRYPTED)

        g.feed(LockEvent.unlock("boot-B", 6))
        g.start_read("cred-1", FileCategory.CREDENTIAL_ENCRYPTED)
        g.finish_read("cred-1")
        self.assertTrue(g.verdict("cred-1").packable)
        self.assertEqual(g.latest_record("cred-1").attempt, 2)

        report = g.pack_check(expected=["dev-1", "cred-1"])
        self.assertFalse(report.packable)  # strict-1 is stale but still tracked
        self.assertEqual(
            {v.shard_id: v.code for v in report.verdicts}["strict-1"],
            VerdictCode.STALE_BOOT,
        )

    def test_unrelated_boot_cannot_inject_state(self):
        g = BackupReadGuard()
        g.feed(PowerEvent.boot("boot-A", 0))
        g.feed(LockEvent.unlock("boot-A", 1))
        g.start_read("cred", FileCategory.CREDENTIAL_ENCRYPTED)
        g.finish_read("cred")

        # A late unlock claiming an old boot id must not refresh permissions.
        g.feed(PowerEvent.boot("boot-B", 2))
        from backup_guard import StaleBootError

        with self.assertRaises(StaleBootError):
            g.feed(LockEvent.unlock("boot-A", 3))
        self.assertFalse(g.state.first_unlock_done)


if __name__ == "__main__":
    unittest.main()
