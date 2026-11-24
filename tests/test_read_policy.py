"""Tests for read acceptance, label upgrade and clearance denial."""

import unittest

from mac_access import AccessMonitor, AccessStatus, Clearance, Label


def build_monitor():
    monitor = AccessMonitor()
    # Process starts at L1, may reach L3 {a,b}.
    monitor.register_process("p", Label(1), Clearance(Label(3, {"a", "b"})))
    monitor.create_object("low", Label(1), b"low-data")
    monitor.create_object("mid", Label(2, {"a"}), b"mid-data")
    monitor.create_object("high", Label(3, {"a", "b"}), b"high-data")
    monitor.create_object("too_high", Label(4), b"top-secret")
    monitor.create_object("forbidden_cat", Label(2, {"z"}), b"cat-z")
    return monitor


class ReadPolicyTests(unittest.TestCase):
    def test_read_equal_label_no_upgrade(self):
        monitor = build_monitor()
        result = monitor.read("p", "low")
        self.assertEqual(result.status, AccessStatus.READ_OK)
        self.assertTrue(result.accepted)
        self.assertFalse(result.upgraded)
        self.assertEqual(result.content, b"low-data")
        self.assertEqual(result.old_label, result.new_label)
        self.assertEqual(monitor.get_process("p").label, Label(1))
        self.assertEqual(result.revoked_handles, ())

    def test_read_higher_label_upgrades_process(self):
        monitor = build_monitor()
        result = monitor.read("p", "mid")
        self.assertEqual(result.status, AccessStatus.READ_OK_UPGRADED)
        self.assertTrue(result.upgraded)
        self.assertEqual(result.content, b"mid-data")
        self.assertEqual(result.new_label, Label(2, {"a"}))
        self.assertEqual(monitor.get_process("p").label, Label(2, {"a"}))

    def test_read_to_clearance_bound_accepted(self):
        monitor = build_monitor()
        result = monitor.read("p", "high")
        self.assertEqual(result.status, AccessStatus.READ_OK_UPGRADED)
        self.assertEqual(result.new_label, Label(3, {"a", "b"}))
        self.assertEqual(result.content, b"high-data")

    def test_read_above_clearance_denied_whole_operation(self):
        monitor = build_monitor()
        result = monitor.read("p", "too_high")
        self.assertEqual(result.status, AccessStatus.READ_DENIED_CLEARANCE)
        self.assertTrue(result.denied)
        self.assertIsNone(result.content)
        # Label untouched, no upgrade, no revocation.
        self.assertEqual(result.old_label, result.new_label)
        self.assertEqual(monitor.get_process("p").label, Label(1))
        self.assertEqual(result.revoked_handles, ())
        self.assertIn("exceeds clearance", result.reason)

    def test_read_with_forbidden_category_denied(self):
        monitor = build_monitor()
        result = monitor.read("p", "forbidden_cat")
        self.assertEqual(result.status, AccessStatus.READ_DENIED_CLEARANCE)
        self.assertIsNone(result.content)
        self.assertEqual(monitor.get_process("p").label, Label(1))

    def test_denied_read_has_no_side_effects_on_mappings(self):
        monitor = build_monitor()
        handle = monitor.open_mapping("p", "low", writable=True)
        result = monitor.read("p", "too_high")
        self.assertTrue(result.denied)
        self.assertFalse(monitor.is_handle_revoked(handle.handle_id))
        self.assertEqual(monitor.get_object("low").content, b"low-data")

    def test_successive_reads_are_monotonic(self):
        monitor = build_monitor()
        monitor.read("p", "mid")  # -> L2 {a}
        # Re-reading a lower object changes nothing.
        again = monitor.read("p", "low")
        self.assertEqual(again.status, AccessStatus.READ_OK)
        self.assertEqual(monitor.get_process("p").label, Label(2, {"a"}))
        # Then up to the bound.
        top = monitor.read("p", "high")
        self.assertEqual(top.new_label, Label(3, {"a", "b"}))
        # Anything beyond is now denied from the raised label too.
        denied = monitor.read("p", "too_high")
        self.assertEqual(denied.status, AccessStatus.READ_DENIED_CLEARANCE)

    def test_category_only_upgrade(self):
        monitor = AccessMonitor()
        monitor.register_process("p", Label(2, {"a"}), Clearance(Label(2, {"a", "b"})))
        monitor.create_object("obj", Label(2, {"b"}), b"x")
        result = monitor.read("p", "obj")
        self.assertEqual(result.status, AccessStatus.READ_OK_UPGRADED)
        self.assertEqual(result.new_label, Label(2, {"a", "b"}))


if __name__ == "__main__":
    unittest.main()
