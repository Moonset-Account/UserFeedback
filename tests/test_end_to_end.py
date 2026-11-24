"""End-to-end policy story matching the task statement, in one assertion chain."""

import unittest

from mac_access import AccessMonitor, AccessStatus, Clearance, Label


class EndToEndScenarioTests(unittest.TestCase):
    def test_classified_read_closes_low_writeback_channel(self):
        monitor = AccessMonitor()

        # A workstation process at L1 cleared to L4 {a,b}.
        monitor.register_process(
            "workstation", Label(1), Clearance(Label(4, {"a", "b"}))
        )

        # Shared memory: a low-side drop box and a high-side vault. The vault
        # label dominates every label the process can ever reach.
        monitor.create_object("dropbox", Label(1), b"dropbox-original")
        monitor.create_object(
            "vault", Label(4, {"a", "b"}), b"vault-original"
        )
        # Higher-classified material the process has not read yet.
        monitor.create_object("dossier", Label(3, {"a"}), b"dossier-text")

        low_handle = monitor.open_mapping(
            "workstation", "dropbox", writable=True
        )
        high_handle = monitor.open_mapping(
            "workstation", "vault", writable=True
        )
        low_dup = monitor.duplicate_handle(low_handle.handle_id)

        # A write-back is queued while the process is still low. It has not
        # arrived when the classified read happens.
        monitor.queue_write_back(
            "workstation", low_handle.handle_id, b"queued-tamper"
        )
        monitor.queue_write_back(
            "workstation", high_handle.handle_id, b"queued-vault-update"
        )

        # The process reads the higher-classified dossier.
        read = monitor.read("workstation", "dossier")
        self.assertEqual(read.status, AccessStatus.READ_OK_UPGRADED)
        self.assertEqual(read.new_label, Label(3, {"a"}))
        self.assertEqual(read.content, b"dossier-text")

        # Both handles onto the low mapping are revoked together; the high
        # mapping still covers the new label and remains active.
        self.assertTrue(low_handle.revoked)
        self.assertTrue(low_dup.revoked)
        self.assertFalse(high_handle.revoked)
        self.assertEqual(
            monitor.mapping_status(low_handle.handle_id),
            AccessStatus.MAPPING_REVOKED,
        )

        # Queued write-backs now arrive and are re-checked against live state.
        results = monitor.flush_pending_writes()
        self.assertEqual(
            [r.status for r in results],
            [AccessStatus.MAPPING_REVOKED, AccessStatus.WRITE_OK],
        )

        # Low target keeps its original content; high target is updated.
        self.assertEqual(
            monitor.get_object("dropbox").content, b"dropbox-original"
        )
        self.assertEqual(
            monitor.get_object("vault").content, b"queued-vault-update"
        )

        # A later attempt through the duplicated low handle is refused as well.
        retry = monitor.write_back(
            "workstation", low_dup.handle_id, b"retry-tamper"
        )
        self.assertEqual(retry.status, AccessStatus.MAPPING_REVOKED)
        self.assertEqual(
            monitor.get_object("dropbox").content, b"dropbox-original"
        )

    def test_over_clearance_read_fails_atomically(self):
        monitor = AccessMonitor()
        monitor.register_process(
            "p", Label(1, {"a"}), Clearance(Label(2, {"a"}))
        )
        monitor.create_object("obj", Label(1, {"a"}), b"v")
        handle = monitor.open_mapping("p", "obj", writable=True)

        # Merging with L3 {a,b} would exceed the L2 {a} clearance.
        monitor.create_object("too_secret", Label(3, {"b"}), b"secret")
        denied = monitor.read("p", "too_secret")
        self.assertEqual(denied.status, AccessStatus.READ_DENIED_CLEARANCE)
        self.assertIsNone(denied.content)
        self.assertEqual(monitor.get_process("p").label, Label(1, {"a"}))
        self.assertFalse(handle.revoked)

        # Channel still works because nothing was upgraded or revoked.
        ok = monitor.write_back("p", handle.handle_id, b"still-ok")
        self.assertEqual(ok.status, AccessStatus.WRITE_OK)
        self.assertEqual(monitor.get_object("obj").content, b"still-ok")


if __name__ == "__main__":
    unittest.main()
