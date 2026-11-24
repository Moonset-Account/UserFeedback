"""Interleaved-operation tests: queued write-backs arriving after an upgrade.

Scenario required by the task statement:

1. A process holds writable shared mappings into a low-side object and a
   high-side object whose labels still cover everything the process can reach.
2. A write-back through the low-side handle is *queued* while the process is
   still low, but has not been delivered yet.
3. The process reads higher-classified material and its label is raised; the
   low-side mapping is revoked while the high-side mapping stays valid.
4. The queued write-backs now arrive.  The low one must be refused and the
   target content must keep its original value; the high-side one completes.
"""

import unittest

from mac_access import AccessMonitor, AccessStatus, Clearance, Label


class InterleavingTests(unittest.TestCase):
    def setUp(self):
        self.monitor = AccessMonitor()
        self.monitor.register_process(
            "p", Label(1), Clearance(Label(4, {"a", "b"}))
        )
        self.monitor.create_object("low", Label(1), b"low-original")
        self.monitor.create_object(
            "high", Label(4, {"a", "b"}), b"high-original"
        )
        self.monitor.create_object("secret", Label(3, {"a"}), b"secret")
        self.low_handle = self.monitor.open_mapping(
            "p", "low", writable=True
        )
        self.high_handle = self.monitor.open_mapping(
            "p", "high", writable=True
        )

    def test_queued_write_arriving_after_upgrade_is_refused(self):
        # 1. Queue the low-side write-back BEFORE any upgrade.
        seq_low = self.monitor.queue_write_back(
            "p", self.low_handle.handle_id, b"low-tamper"
        )
        self.assertEqual(seq_low, 1)
        self.assertEqual(len(self.monitor.pending_writes()), 1)

        # 2. Upgrade the process by reading higher material: L1 -> L3 {a}.
        read_result = self.monitor.read("p", "secret")
        self.assertEqual(read_result.status, AccessStatus.READ_OK_UPGRADED)
        self.assertEqual(read_result.new_label, Label(3, {"a"}))
        self.assertTrue(self.low_handle.revoked)
        self.assertFalse(self.high_handle.revoked)

        # 3. The queued request now arrives.
        results = self.monitor.flush_pending_writes()
        self.assertEqual(len(results), 1)
        write_result = results[0]
        self.assertEqual(write_result.status, AccessStatus.MAPPING_REVOKED)
        self.assertEqual(write_result.handle_id, self.low_handle.handle_id)
        # Target content must remain the original value.
        self.assertEqual(
            self.monitor.get_object("low").content, b"low-original"
        )
        self.assertEqual(self.monitor.pending_writes(), ())

    def test_queued_write_to_still_dominating_target_completes(self):
        # Queue both write-backs while low.
        self.monitor.queue_write_back(
            "p", self.low_handle.handle_id, b"low-tamper"
        )
        seq_high = self.monitor.queue_write_back(
            "p", self.high_handle.handle_id, b"high-new"
        )
        self.assertEqual(seq_high, 2)

        # Upgrade happens before either request is delivered.
        self.monitor.read("p", "secret")

        results = self.monitor.flush_pending_writes()
        statuses = [r.status for r in results]
        self.assertEqual(
            statuses,
            [AccessStatus.MAPPING_REVOKED, AccessStatus.WRITE_OK],
        )
        # Low target untouched, high target updated.
        self.assertEqual(
            self.monitor.get_object("low").content, b"low-original"
        )
        self.assertEqual(
            self.monitor.get_object("high").content, b"high-new"
        )

    def test_queued_write_delivered_before_upgrade_succeeds(self):
        # Same queued request, but it arrives while the mapping is still valid.
        self.monitor.queue_write_back(
            "p", self.low_handle.handle_id, b"low-early"
        )
        early = self.monitor.flush_pending_writes()
        self.assertEqual(early[0].status, AccessStatus.WRITE_OK)
        self.assertEqual(
            self.monitor.get_object("low").content, b"low-early"
        )
        # Upgrade afterwards revokes the map but cannot undo the earlier write.
        self.monitor.read("p", "secret")
        self.assertTrue(self.low_handle.revoked)
        self.assertEqual(
            self.monitor.get_object("low").content, b"low-early"
        )

    def test_queued_write_through_duplicated_handle_also_refused(self):
        dup = self.monitor.duplicate_handle(self.low_handle.handle_id)
        self.monitor.queue_write_back("p", dup.handle_id, b"dup-tamper")
        self.monitor.read("p", "secret")
        results = self.monitor.flush_pending_writes()
        self.assertEqual(results[0].status, AccessStatus.MAPPING_REVOKED)
        self.assertEqual(
            self.monitor.get_object("low").content, b"low-original"
        )

    def test_fifo_delivery_order_preserved_across_upgrade(self):
        # Three queued writes; upgrade happens between queueing and delivery.
        h2 = self.monitor.open_mapping("p", "high", writable=True)
        self.monitor.queue_write_back(
            "p", self.low_handle.handle_id, b"w1"
        )
        self.monitor.queue_write_back("p", h2.handle_id, b"w2")
        self.monitor.queue_write_back(
            "p", self.low_handle.handle_id, b"w3"
        )
        self.monitor.read("p", "secret")
        results = self.monitor.flush_pending_writes()
        self.assertEqual(
            [r.status for r in results],
            [
                AccessStatus.MAPPING_REVOKED,
                AccessStatus.WRITE_OK,
                AccessStatus.MAPPING_REVOKED,
            ],
        )
        self.assertEqual(
            [r.content for r in results], [b"w1", b"w2", b"w3"]
        )
        self.assertEqual(
            self.monitor.get_object("high").content, b"w2"
        )
        self.assertEqual(
            self.monitor.get_object("low").content, b"low-original"
        )

    def test_direct_write_after_upgrade_matches_queued_semantics(self):
        self.monitor.read("p", "secret")
        low_result = self.monitor.write_back(
            "p", self.low_handle.handle_id, b"x"
        )
        high_result = self.monitor.write_back(
            "p", self.high_handle.handle_id, b"y"
        )
        self.assertEqual(low_result.status, AccessStatus.MAPPING_REVOKED)
        self.assertEqual(high_result.status, AccessStatus.WRITE_OK)
        self.assertEqual(
            self.monitor.get_object("low").content, b"low-original"
        )
        self.assertEqual(self.monitor.get_object("high").content, b"y")

    def test_denied_read_does_not_revoke_so_queued_write_proceeds(self):
        # A read that fails the clearance must not alter labels or mappings,
        # so a queued low-side write still applies when delivered.
        self.monitor.create_object("beyond", Label(5), b"beyond")
        self.monitor.queue_write_back(
            "p", self.low_handle.handle_id, b"still-low"
        )
        denied = self.monitor.read("p", "beyond")
        self.assertEqual(
            denied.status, AccessStatus.READ_DENIED_CLEARANCE
        )
        results = self.monitor.flush_pending_writes()
        self.assertEqual(results[0].status, AccessStatus.WRITE_OK)
        self.assertEqual(
            self.monitor.get_object("low").content, b"still-low"
        )


if __name__ == "__main__":
    unittest.main()
