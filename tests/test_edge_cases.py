"""Edge cases: API errors, multi-process isolation, mapping-open policy."""

import unittest

from mac_access import (
    AccessControlError,
    AccessMonitor,
    AccessStatus,
    Clearance,
    ForeignHandleError,
    Label,
    UnknownHandleError,
    UnknownObjectError,
    UnknownProcessError,
)


class EdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.monitor = AccessMonitor()
        self.monitor.register_process(
            "p1", Label(1), Clearance(Label(3))
        )
        self.monitor.register_process(
            "p2", Label(1), Clearance(Label(3))
        )
        self.monitor.create_object("low", Label(1), b"l")
        self.monitor.create_object("high", Label(3), b"h")

    def test_unknown_entities_raise_named_errors(self):
        with self.assertRaises(UnknownProcessError):
            self.monitor.read("ghost", "low")
        with self.assertRaises(UnknownObjectError):
            self.monitor.read("p1", "ghost")
        with self.assertRaises(UnknownHandleError):
            self.monitor.write_back("p1", "h-999", b"x")
        with self.assertRaises(UnknownHandleError):
            self.monitor.is_handle_revoked("h-999")

    def test_duplicate_registration_rejected(self):
        with self.assertRaises(ValueError):
            self.monitor.register_process("p1", Label(0), Clearance(Label(1)))
        with self.assertRaises(ValueError):
            self.monitor.create_object("low", Label(0))

    def test_open_mapping_requires_target_to_cover_process_label(self):
        # p1 is L1; a fresh L0 object is below the process label.
        self.monitor.create_object("below", Label(0), b"b")
        with self.assertRaises(AccessControlError):
            self.monitor.open_mapping("p1", "below", writable=True)
        with self.assertRaises(AccessControlError):
            self.monitor.open_mapping("p1", "below", writable=False)

    def test_foreign_handle_rejected(self):
        handle = self.monitor.open_mapping("p1", "low", writable=True)
        with self.assertRaises(ForeignHandleError):
            self.monitor.write_back("p2", handle.handle_id, b"x")
        with self.assertRaises(ForeignHandleError):
            self.monitor.queue_write_back("p2", handle.handle_id, b"x")

    def test_processes_are_isolated_on_revocation(self):
        h1 = self.monitor.open_mapping("p1", "low", writable=True)
        h2 = self.monitor.open_mapping("p2", "low", writable=True)
        self.monitor.create_object("mid", Label(2), b"m")
        result = self.monitor.read("p1", "mid")
        self.assertIn(h1.handle_id, result.revoked_handles)
        # p2 never upgraded: its mapping stays active and writable.
        self.assertFalse(h2.revoked)
        ok = self.monitor.write_back("p2", h2.handle_id, b"p2-write")
        self.assertEqual(ok.status, AccessStatus.WRITE_OK)
        self.assertEqual(self.monitor.get_object("low").content, b"p2-write")
        # p1 still cannot write through its revoked handle.
        refused = self.monitor.write_back("p1", h1.handle_id, b"p1-write")
        self.assertEqual(refused.status, AccessStatus.MAPPING_REVOKED)
        self.assertEqual(self.monitor.get_object("low").content, b"p2-write")

    def test_unknown_handle_after_foreign_check_message(self):
        handle = self.monitor.open_mapping("p1", "low", writable=True)
        try:
            self.monitor.write_back("p2", handle.handle_id, b"x")
        except ForeignHandleError as exc:
            self.assertIn("p2", str(exc))
        else:
            self.fail("ForeignHandleError expected")

    def test_invalid_content_types(self):
        handle = self.monitor.open_mapping("p1", "low", writable=True)
        with self.assertRaises(TypeError):
            self.monitor.write_back("p1", handle.handle_id, "str")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.monitor.queue_write_back("p1", handle.handle_id, 123)  # type: ignore[arg-type]

    def test_bytearray_is_accepted_and_normalised(self):
        handle = self.monitor.open_mapping("p1", "low", writable=True)
        result = self.monitor.write_back(
            "p1", handle.handle_id, bytearray(b"bytes-ok")
        )
        self.assertEqual(result.status, AccessStatus.WRITE_OK)
        self.assertEqual(
            self.monitor.get_object("low").content, b"bytes-ok"
        )
        self.assertIsInstance(self.monitor.get_object("low").content, bytes)

    def test_three_way_status_distinction(self):
        # 1. Denied read.
        self.monitor.create_object("too_high", Label(4), b"t")
        denied = self.monitor.read("p1", "too_high")
        self.assertEqual(
            denied.status, AccessStatus.READ_DENIED_CLEARANCE
        )
        self.assertTrue(denied.denied)

        # 2. Accepted read that upgrades and revokes a mapping.
        handle = self.monitor.open_mapping("p1", "low", writable=True)
        self.monitor.create_object("mid", Label(2), b"m")
        upgraded = self.monitor.read("p1", "mid")
        self.assertEqual(upgraded.status, AccessStatus.READ_OK_UPGRADED)
        self.assertTrue(upgraded.upgraded)
        self.assertTrue(handle.revoked)

        # 3a. Refused write-back (revoked channel).
        refused = self.monitor.write_back("p1", handle.handle_id, b"x")
        self.assertEqual(refused.status, AccessStatus.MAPPING_REVOKED)
        self.assertTrue(refused.refused)

        # 3b. Successful write-back through a still-dominating high target.
        high_handle = self.monitor.open_mapping(
            "p1", "high", writable=True
        )
        ok = self.monitor.write_back("p1", high_handle.handle_id, b"done")
        self.assertEqual(ok.status, AccessStatus.WRITE_OK)
        self.assertTrue(ok.applied)

        # Every status is a distinct, string-serializable value.
        values = {s.value for s in AccessStatus}
        self.assertEqual(
            values,
            {
                "read_ok",
                "read_ok_upgraded",
                "read_denied_clearance",
                "mapping_revoked",
                "write_ok",
            },
        )

    def test_repeated_flush_is_empty(self):
        handle = self.monitor.open_mapping("p1", "low", writable=True)
        self.monitor.queue_write_back("p1", handle.handle_id, b"z")
        first = self.monitor.flush_pending_writes()
        second = self.monitor.flush_pending_writes()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])


if __name__ == "__main__":
    unittest.main()
