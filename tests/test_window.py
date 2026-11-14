"""Unit tests for the rolling-window primitive."""

import unittest

from quota.window import RollingWindow


class RollingWindowTests(unittest.TestCase):
    def test_slot_is_released_exactly_at_window_end(self) -> None:
        window = RollingWindow(window_seconds=10.0)
        window.record(0.0)
        window.prune(9.999)
        self.assertEqual(window.count(), 1)
        window.prune(10.0)
        self.assertEqual(window.count(), 0)

    def test_expire_at_points_at_oldest_plus_window(self) -> None:
        window = RollingWindow(window_seconds=5.0)
        self.assertEqual(window.expire_at(2.0), 2.0)
        window.record(2.0)
        window.record(3.0)
        self.assertEqual(window.expire_at(3.0), 7.0)

    def test_multiple_events_expire_in_order(self) -> None:
        window = RollingWindow(window_seconds=10.0)
        for stamp in (0.0, 4.0, 9.0):
            window.record(stamp)
        window.prune(10.0)
        self.assertEqual(list(window.events()), [4.0, 9.0])
        window.prune(20.0)
        self.assertEqual(window.count(), 0)

    def test_monotonic_timestamps_enforced(self) -> None:
        window = RollingWindow(window_seconds=10.0)
        window.record(5.0)
        with self.assertRaises(AssertionError):
            window.record(4.0)


if __name__ == "__main__":
    unittest.main()
