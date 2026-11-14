"""Requirement-mapped tests for the temporary diagnostic probe controller.

Case id mapping (SEC-20261114-014):
  SEC-014-REQ-01  open only when device config version matches the approval
  SEC-014-REQ-02  successful open records original value and written version
  SEC-014-REQ-03  expiry restores the original value while version is intact
  SEC-014-REQ-04  human change during the window escalates to manual handling
  SEC-014-REQ-05  reconnect drains already-expired windows first
  SEC-014-REQ-06  controller restart recovers unclosed windows
  SEC-014-REQ-07  duplicate receipts cause only one state migration
  SEC-014-REQ-08  repeated expiry processing stays idempotent once closed
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from controller import (  # noqa: E402
    CLOSED_RESTORED,
    ESCALATED,
    OPEN,
    RESTORE_PENDING,
    JsonStore,
    ProbeWindowController,
    SimulatedDeviceWorld,
)

T0 = "2026-11-14T09:00:00"
T_END = "2026-11-14T10:00:00"
T_BEFORE_END = "2026-11-14T09:59:59"
T_AFTER_END = "2026-11-14T10:00:01"


class ControllerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = JsonStore(self.tmp.name)
        self.controller = ProbeWindowController(self.store)
        self.world = SimulatedDeviceWorld(self.store)
        self.world.register(
            "dev-1", "v7",
            {"temp": "20", "pressure": "1", "humidity": "40"})

    def open_default(self, **overrides):
        args = dict(device_id="dev-1", probe="temp", value="ro-diag",
                    work_order="WO-1", approved_version="v7",
                    start=T0, end=T_END, now=T0)
        args.update(overrides)
        return self.controller.open_window(**args)

    def window(self, window_id):
        return self.controller.status()["windows"][window_id]

    def device(self, device_id="dev-1"):
        return self.controller.status()["devices"][device_id]


class TestOpenGating(ControllerTestBase):
    def test_REQ01_open_accepted_when_version_matches(self):
        """SEC-014-REQ-01: matching approved version opens the window."""
        result = self.open_default()
        self.assertTrue(result["accepted"])
        window = result["window"]
        self.assertEqual(OPEN, window["state"])
        self.assertEqual("ro-diag", self.device()["probes"]["temp"])

    def test_REQ01_open_rejected_when_version_mismatches(self):
        """SEC-014-REQ-01: stale approved version rejects and touches nothing."""
        result = self.open_default(approved_version="v6")
        self.assertFalse(result["accepted"])
        self.assertEqual("version-mismatch", result["reason"])
        self.assertEqual("v7", result["actual_version"])
        # No window persisted, device untouched.
        self.assertEqual({}, self.controller.status()["windows"])
        self.assertEqual("v7", self.device()["version"])
        self.assertEqual("20", self.device()["probes"]["temp"])

    def test_REQ01_open_rejected_when_device_offline(self):
        """SEC-014-REQ-01: an offline device cannot be version-checked."""
        self.world.set_online("dev-1", False)
        result = self.open_default()
        self.assertFalse(result["accepted"])
        self.assertEqual("device-offline", result["reason"])
        self.assertEqual({}, self.controller.status()["windows"])

    def test_REQ01_open_rejected_when_window_invalid(self):
        """SEC-014-REQ-01: end must be after start."""
        result = self.open_default(end=T0)
        self.assertFalse(result["accepted"])
        self.assertEqual("invalid-window", result["reason"])


class TestOpenRecording(ControllerTestBase):
    def test_REQ02_open_records_original_value_and_written_version(self):
        """SEC-014-REQ-02: original value and written version are recorded."""
        result = self.open_default()
        window = result["window"]
        self.assertEqual("20", window["original_value"])
        self.assertEqual("managed-" + window["window_id"], window["written_version"])
        # The device now carries this window's written version token.
        self.assertEqual(window["written_version"],
                         self.device()["marks"]["temp"])
        # The managed baseline version is not disturbed by a controller write.
        self.assertEqual("v7", self.device()["version"])


class TestExpiryRestore(ControllerTestBase):
    def test_REQ03_expiry_restores_original_value(self):
        """SEC-014-REQ-03: untouched window restores the original value."""
        window_id = self.open_default()["window"]["window_id"]
        actions = self.controller.process_due(now=T_AFTER_END)
        self.assertEqual([{"window_id": window_id, "action": "restored",
                           "state": CLOSED_RESTORED}], actions)
        self.assertEqual(CLOSED_RESTORED, self.window(window_id)["state"])
        self.assertEqual("20", self.device()["probes"]["temp"])
        self.assertIsNone(self.device()["marks"]["temp"])
        self.assertEqual("v7", self.device()["version"])

    def test_REQ03_boundary_exactly_at_end_expires(self):
        """SEC-014-REQ-03 (boundary): end <= now means the window is due."""
        window_id = self.open_default()["window"]["window_id"]
        actions = self.controller.process_due(now=T_END)
        self.assertEqual("restored", actions[0]["action"])
        self.assertEqual(CLOSED_RESTORED, self.window(window_id)["state"])

    def test_REQ03_boundary_one_second_before_end_stays_open(self):
        """SEC-014-REQ-03 (boundary): window is not due before its end."""
        window_id = self.open_default()["window"]["window_id"]
        actions = self.controller.process_due(now=T_BEFORE_END)
        self.assertEqual([], actions)
        self.assertEqual(OPEN, self.window(window_id)["state"])
        self.assertEqual("ro-diag", self.device()["probes"]["temp"])


class TestHumanChangeEscalation(ControllerTestBase):
    def test_REQ04_human_change_during_window_escalates(self):
        """SEC-014-REQ-04: out-of-band human change blocks auto-restore."""
        window_id = self.open_default()["window"]["window_id"]
        self.world.human_edit("dev-1", "temp", "99")
        actions = self.controller.process_due(now=T_AFTER_END)
        self.assertEqual("escalated", actions[0]["action"])
        self.assertEqual(ESCALATED, self.window(window_id)["state"])
        # The human's value is left untouched for manual handling.
        self.assertEqual("99", self.device()["probes"]["temp"])
        self.assertEqual("human-1", self.device()["version"])
        self.assertEqual("version-changed-externally",
                         self.window(window_id)["history"][-1]["reason"])

    def test_REQ04_human_change_on_other_probe_still_escalates(self):
        """SEC-014-REQ-04: any human config change during the window escalates."""
        window_id = self.open_default()["window"]["window_id"]
        self.world.human_edit("dev-1", "pressure", "5")
        actions = self.controller.process_due(now=T_AFTER_END)
        self.assertEqual("escalated", actions[0]["action"])
        self.assertEqual(ESCALATED, self.window(window_id)["state"])
        self.assertEqual("ro-diag", self.device()["probes"]["temp"])


class TestReconnectPriority(ControllerTestBase):
    def test_REQ05_reconnect_drains_expired_windows_first(self):
        """SEC-014-REQ-05: expired windows are handled on reconnect, earliest
        expiry first, before any not-yet-expired window."""
        win_a = self.open_default(
            probe="temp", work_order="WO-A", end="2026-11-14T10:00:00",
        )["window"]["window_id"]
        win_b = self.open_default(
            probe="pressure", value="ro-p", work_order="WO-B",
            end="2026-11-14T11:00:00",
        )["window"]["window_id"]
        win_c = self.open_default(
            probe="humidity", value="ro-h", work_order="WO-C",
            end="2026-11-14T12:00:00",
        )["window"]["window_id"]

        self.world.set_online("dev-1", False)
        deferred = self.controller.process_due(now="2026-11-14T11:30:00")
        self.assertEqual([win_a, win_b],
                         [a["window_id"] for a in deferred])
        self.assertTrue(all(a["action"] == "deferred-offline" for a in deferred))
        self.assertEqual(RESTORE_PENDING, self.window(win_a)["state"])
        self.assertEqual(RESTORE_PENDING, self.window(win_b)["state"])

        actions = self.controller.on_reconnect(
            "dev-1", now="2026-11-14T11:30:00")
        # Expired windows only, earliest expiry first; win_c is untouched.
        self.assertEqual([win_a, win_b], [a["window_id"] for a in actions])
        self.assertTrue(all(a["action"] == "restored" for a in actions))
        self.assertEqual(CLOSED_RESTORED, self.window(win_a)["state"])
        self.assertEqual(CLOSED_RESTORED, self.window(win_b)["state"])
        self.assertEqual(OPEN, self.window(win_c)["state"])
        self.assertEqual("20", self.device()["probes"]["temp"])
        self.assertEqual("1", self.device()["probes"]["pressure"])
        self.assertEqual("ro-h", self.device()["probes"]["humidity"])

    def test_REQ05_reconnect_with_human_change_escalates_instead(self):
        """SEC-014-REQ-05+04: expired window whose probe was touched by a
        human while offline escalates on reconnect rather than restoring."""
        window_id = self.open_default()["window"]["window_id"]
        self.world.set_online("dev-1", False)
        self.world.human_edit("dev-1", "temp", "77")  # local console access
        actions = self.controller.on_reconnect("dev-1", now=T_AFTER_END)
        self.assertEqual("escalated", actions[0]["action"])
        self.assertEqual(ESCALATED, self.window(window_id)["state"])
        self.assertEqual("77", self.device()["probes"]["temp"])


class TestRestartRecovery(ControllerTestBase):
    def test_REQ06_restart_recovers_unclosed_windows(self):
        """SEC-014-REQ-06: a fresh controller on the same state dir finds the
        open window and closes it on expiry."""
        window_id = self.open_default()["window"]["window_id"]

        # Simulate a controller restart: brand-new instances, same state dir.
        restarted = ProbeWindowController(JsonStore(self.tmp.name))
        status = restarted.status()
        self.assertIn(window_id, status["windows"])
        self.assertEqual(OPEN, status["windows"][window_id]["state"])

        actions = restarted.process_due(now=T_AFTER_END)
        self.assertEqual("restored", actions[0]["action"])
        self.assertEqual(CLOSED_RESTORED,
                         restarted.status()["windows"][window_id]["state"])
        self.assertEqual("20", restarted.status()["devices"]["dev-1"]["probes"]["temp"])


class TestReceiptIdempotency(ControllerTestBase):
    def test_REQ07_duplicate_open_receipt_causes_single_migration(self):
        """SEC-014-REQ-07: re-delivering the open receipt is a no-op."""
        result = self.open_default()
        window_id = result["window"]["window_id"]
        self.assertTrue(result["receipt"]["applied"])
        history_len = len(self.window(window_id)["history"])

        receipts = self.store.load("receipts.json", {})
        open_receipt = receipts["rcpt-{}-open".format(window_id)]["receipt"]
        again = self.controller.apply_receipt(open_receipt, now=T0)
        self.assertFalse(again["applied"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(OPEN, self.window(window_id)["state"])
        self.assertEqual(history_len, len(self.window(window_id)["history"]))

    def test_REQ07_duplicate_restore_receipt_causes_single_migration(self):
        """SEC-014-REQ-07: exactly one transition to CLOSED_RESTORED."""
        window_id = self.open_default()["window"]["window_id"]
        self.controller.process_due(now=T_AFTER_END)

        receipts = self.store.load("receipts.json", {})
        restore_receipt = receipts["rcpt-{}-restore".format(window_id)]["receipt"]
        again = self.controller.apply_receipt(restore_receipt, now=T_AFTER_END)
        self.assertFalse(again["applied"])
        self.assertTrue(again["duplicate"])

        transitions = [h for h in self.window(window_id)["history"]
                       if h["to"] == CLOSED_RESTORED]
        self.assertEqual(1, len(transitions))
        self.assertEqual(CLOSED_RESTORED, self.window(window_id)["state"])


class TestExpiryIdempotency(ControllerTestBase):
    def test_REQ08_repeat_tick_after_close_is_noop(self):
        """SEC-014-REQ-08: once closed, further ticks change nothing."""
        window_id = self.open_default()["window"]["window_id"]
        self.controller.process_due(now=T_AFTER_END)
        snapshot = self.window(window_id)
        actions = self.controller.process_due(now="2026-11-14T23:59:59")
        self.assertEqual([], actions)
        self.assertEqual(snapshot, self.window(window_id))
        self.assertEqual("20", self.device()["probes"]["temp"])


if __name__ == "__main__":
    unittest.main()
