import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from lacp import ManualClock


class ManualClockTest(unittest.TestCase):
    def test_starts_at_zero_by_default(self):
        clock = ManualClock()
        self.assertEqual(clock.now(), 0.0)

    def test_starts_at_given_time(self):
        clock = ManualClock(100.0)
        self.assertEqual(clock.now(), 100.0)

    def test_advance_accumulates(self):
        clock = ManualClock()
        self.assertEqual(clock.advance(5), 5.0)
        self.assertEqual(clock.advance(2.5), 7.5)
        self.assertEqual(clock.now(), 7.5)

    def test_tick_is_alias_of_advance(self):
        clock = ManualClock()
        clock.tick(3)
        self.assertEqual(clock.now(), 3.0)

    def test_cannot_move_backwards(self):
        clock = ManualClock()
        with self.assertRaises(ValueError):
            clock.advance(-1)


if __name__ == "__main__":
    unittest.main()
