"""Tests for writable-shared-mapping revocation on label upgrade."""

import unittest

from mac_access import AccessMonitor, AccessStatus, Clearance, Label


class RevocationTests(unittest.TestCase):
    def setUp(self):
        self.monitor = AccessMonitor()
        self.monitor.register_process(
            "p", Label(1), Clearance(Label(4, {"a", "b", "c"}))
        )
        # Low target: L1 -- cannot cover an upgraded process label.
        self.monitor.create_object("low", Label(1), b"original-low")
        # High target: L4 {a,b,c} -- still dominates every label the
        # process can reach, so its mapping must survive the upgrade.
        self.monitor.create_object(
            "high", Label(4, {"a", "b", "c"}), b"original-high"
        )
        self.low_handle = self.monitor.open_mapping(
            "p", "low", writable=True
        )
        self.high_handle = self.monitor.open_mapping(
            "p", "high", writable=True
        )
        self.monitor.create_object("mid", Label(2), b"mid")

    def test_upgrade_revokes_only_unsatisfied_writable_maps(self):
        result = self.monitor.read("p", "mid")  # L1 -> L2
        self.assertEqual(result.status, AccessStatus.READ_OK_UPGRADED)
        self.assertIn(self.low_handle.handle_id, result.revoked_handles)
        self.assertNotIn(self.high_handle.handle_id, result.revoked_handles)
        self.assertTrue(self.monitor.is_handle_revoked(self.low_handle.handle_id))
        self.assertFalse(self.monitor.is_handle_revoked(self.high_handle.handle_id))
        self.assertEqual(
            self.monitor.mapping_status(self.low_handle.handle_id),
            AccessStatus.MAPPING_REVOKED,
        )

    def test_duplicated_handles_share_revocation(self):
        dup_a = self.monitor.duplicate_handle(self.low_handle.handle_id)
        dup_b = self.monitor.duplicate_handle(dup_a.handle_id)
        self.monitor.read("p", "mid")
        for handle in (self.low_handle, dup_a, dup_b):
            self.assertTrue(handle.revoked)
            self.assertIsNotNone(handle.revoke_reason)
            self.assertTrue(self.monitor.is_handle_revoked(handle.handle_id))

    def test_high_target_mapping_keeps_working_after_upgrade(self):
        self.monitor.read("p", "mid")  # L1 -> L2
        result = self.monitor.write_back(
            "p", self.high_handle.handle_id, b"new-high"
        )
        self.assertEqual(result.status, AccessStatus.WRITE_OK)
        self.assertTrue(result.applied)
        self.assertEqual(self.monitor.get_object("high").content, b"new-high")

    def test_write_through_revoked_handle_leaves_content_unchanged(self):
        self.monitor.read("p", "mid")
        result = self.monitor.write_back(
            "p", self.low_handle.handle_id, b"tampered"
        )
        self.assertEqual(result.status, AccessStatus.MAPPING_REVOKED)
        self.assertTrue(result.refused)
        self.assertIn("no longer dominates", result.reason)
        self.assertEqual(
            self.monitor.get_object("low").content, b"original-low"
        )

    def test_read_only_mapping_is_not_revoked(self):
        ro = self.monitor.open_mapping("p", "low", writable=False)
        result = self.monitor.read("p", "mid")
        self.assertNotIn(ro.handle_id, result.revoked_handles)
        self.assertFalse(ro.revoked)

    def test_read_only_mapping_rejects_write_back_as_usage_error(self):
        ro = self.monitor.open_mapping("p", "low", writable=False)
        from mac_access import AccessControlError

        with self.assertRaises(AccessControlError):
            self.monitor.write_back("p", ro.handle_id, b"x")
        self.assertEqual(
            self.monitor.get_object("low").content, b"original-low"
        )

    def test_revocation_runs_again_on_every_upgrade(self):
        # First upgrade L1 -> L2 revokes the L1 map only.
        first = self.monitor.read("p", "mid")
        self.assertEqual(len(first.revoked_handles), 1)

        # Add a fresh L3 writable target, map it, then upgrade past it.
        self.monitor.create_object("l3", Label(3), b"l3")
        l3_handle = self.monitor.open_mapping("p", "l3", writable=True)
        self.monitor.create_object("l3b", Label(3, {"a"}), b"l3b")
        second = self.monitor.read("p", "l3b")  # L2 -> L3 {a}
        self.assertIn(l3_handle.handle_id, second.revoked_handles)
        self.assertNotIn(self.high_handle.handle_id, second.revoked_handles)
        self.assertTrue(l3_handle.revoked)

    def test_no_upgrade_means_no_revocation(self):
        result = self.monitor.read("p", "low")  # equal label, no change
        self.assertEqual(result.status, AccessStatus.READ_OK)
        self.assertEqual(result.revoked_handles, ())
        self.assertFalse(self.low_handle.revoked)

    def test_revoked_map_stays_revoked_on_later_upgrades(self):
        self.monitor.read("p", "mid")
        self.assertTrue(self.low_handle.revoked)
        # Further upgrade does not resurrect anything.
        self.monitor.create_object("l3", Label(3), b"l3")
        self.monitor.read("p", "l3")
        self.assertTrue(self.low_handle.revoked)
        self.assertEqual(
            self.monitor.get_object("low").content, b"original-low"
        )

    def test_category_based_revocation(self):
        monitor = AccessMonitor()
        monitor.register_process(
            "q", Label(1, {"a"}), Clearance(Label(1, {"a", "b"}))
        )
        monitor.create_object("cat_obj", Label(1, {"a"}), b"cat")
        handle = monitor.open_mapping("q", "cat_obj", writable=True)
        monitor.create_object("add_b", Label(1, {"b"}), b"b")
        result = monitor.read("q", "add_b")  # adds category b
        self.assertEqual(result.new_label, Label(1, {"a", "b"}))
        # Target {a} no longer dominates {a,b}; writable map is revoked.
        self.assertTrue(handle.revoked)
        refused = monitor.write_back("q", handle.handle_id, b"x")
        self.assertEqual(refused.status, AccessStatus.MAPPING_REVOKED)
        self.assertEqual(monitor.get_object("cat_obj").content, b"cat")


if __name__ == "__main__":
    unittest.main()
