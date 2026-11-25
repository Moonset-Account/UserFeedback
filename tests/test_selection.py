import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from lacp import LacpController, ManualClock


def bring_group(controller, ports, system_id, key, ttl=30.0, synced=True):
    """Bring ports up and feed them matching synchronized advertisements."""
    for pid in ports:
        controller.port_up(pid)
        controller.receive_advertisement(pid, system_id, key, synced, ttl=ttl)


class SelectionTest(unittest.TestCase):
    def test_largest_group_is_selected_and_collecting(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2], "A", 100)
        bring_group(c, [3, 4, 5], "B", 200)

        self.assertEqual(c.selected_group, ("B", 200))
        self.assertEqual(c.collecting_ports(), frozenset({3, 4, 5}))
        for pid in (3, 4, 5):
            self.assertTrue(c.can_collect(pid))
        for pid in (1, 2):
            self.assertFalse(c.can_collect(pid))

    def test_tie_without_incumbent_uses_identifier_order(self):
        # Port-by-port events always promote the first eligible group to
        # incumbent, so the no-incumbent tie path is exercised against the
        # selection rule directly with groups presented simultaneously.
        c = LacpController(clock=ManualClock())
        gid, members = c._choose({("B", 200): {1}, ("A", 200): {2}})
        self.assertEqual(gid, ("A", 200))
        self.assertEqual(members, frozenset({2}))

    def test_choose_tie_breaks_on_identifier_without_incumbent(self):
        c = LacpController(clock=ManualClock())
        self.assertIsNone(c._selected_id)
        gid, members = c._choose(
            {("B", 200): {1}, ("A", 200): {2}, ("A", 100): {3}}
        )
        # Lowest identifier among the equal-size (single-member) groups.
        self.assertEqual(gid, ("A", 100))
        self.assertEqual(members, frozenset({3}))

    def test_choose_incumbent_wins_otherwise_equal_tie(self):
        c = LacpController(clock=ManualClock())
        c._selected_id = ("B", 200)
        gid, _ = c._choose({("B", 200): {1}, ("A", 200): {2}})
        self.assertEqual(gid, ("B", 200))

    def test_tie_keeps_incumbent_group(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2], "A", 100)
        bring_group(c, [3, 4], "B", 200)
        self.assertEqual(c.selected_group, ("A", 100))

        # Equal-size challenger must not dislodge the incumbent.
        bring_group(c, [5, 6], "C", 300)
        self.assertEqual(c.selected_group, ("A", 100))
        self.assertEqual(c.collecting_ports(), frozenset({1, 2}))

    def test_larger_challenger_dislodges_incumbent(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2], "A", 100)
        bring_group(c, [3], "B", 200)
        self.assertEqual(c.selected_group, ("A", 100))

        bring_group(c, [4, 5], "B", 200)
        self.assertEqual(c.selected_group, ("B", 200))
        self.assertEqual(c.collecting_ports(), frozenset({3, 4, 5}))

    def test_different_keys_split_groups_even_for_same_system(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2], "A", 100)
        bring_group(c, [3], "A", 999)
        self.assertEqual(c.selected_group, ("A", 100))
        self.assertEqual(c.collecting_ports(), frozenset({1, 2}))

    def test_unsynced_advertisement_is_not_syncable(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2], "A", 100, synced=False)
        self.assertIsNone(c.selected_group)
        self.assertEqual(c.collecting_ports(), frozenset())

    def test_mixed_synced_and_unsynced_members(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2], "A", 100, synced=True)
        c.port_up(3)
        c.receive_advertisement(3, "A", 100, synced=False)
        # Only synchronized members belong to the syncable group.
        self.assertEqual(c.selected_group, ("A", 100))
        self.assertEqual(c.collecting_ports(), frozenset({1, 2}))
        self.assertFalse(c.can_collect(3))

        # Port 3 finishing synchronization joins the same group.
        c.receive_advertisement(3, "A", 100, synced=True)
        self.assertEqual(c.collecting_ports(), frozenset({1, 2, 3}))

    def test_moving_port_between_groups_migrates_membership(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2], "A", 100)
        bring_group(c, [3], "B", 200)
        self.assertEqual(c.selected_group, ("A", 100))

        # Port 3 advertises membership of group A, making it the only group.
        c.receive_advertisement(3, "A", 100, synced=True)
        self.assertEqual(c.selected_group, ("A", 100))
        self.assertEqual(c.collecting_ports(), frozenset({1, 2, 3}))

    def test_no_candidate_means_no_group_and_no_collection(self):
        c = LacpController(clock=ManualClock())
        c.port_up(1)
        self.assertIsNone(c.selected_group)
        self.assertFalse(c.can_collect(1))


if __name__ == "__main__":
    unittest.main()
