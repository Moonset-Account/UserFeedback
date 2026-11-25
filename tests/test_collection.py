import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from lacp import LacpController, ManualClock


def bring_group(controller, ports, system_id="A", key=100, ttl=30.0, synced=True):
    for pid in ports:
        controller.port_up(pid)
        controller.receive_advertisement(pid, system_id, key, synced, ttl=ttl)


class CollectionTest(unittest.TestCase):
    def test_only_selected_synced_members_collect(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2])
        c.port_up(3)  # no advertisement
        self.assertEqual(c.collecting_ports(), frozenset({1, 2}))
        self.assertFalse(c.can_collect(3))
        self.assertFalse(c.can_collect(99))  # unknown port

    def test_advertisement_expiry_removes_collection_only(self):
        c = LacpController(ttl=10.0, clock=ManualClock())
        bring_group(c, [1, 2], ttl=10.0)
        self.assertEqual(c.collecting_ports(), frozenset({1, 2}))

        c.tick(10)  # deadline reached: advertisements expire (now >= deadline)
        self.assertEqual(c.collecting_ports(), frozenset())
        self.assertIsNone(c.selected_group)

        # Re-advertising restores collection without a generation bump beyond
        # the expiry-driven one.
        c.receive_advertisement(1, "A", 100, True)
        c.receive_advertisement(2, "A", 100, True)
        self.assertEqual(c.collecting_ports(), frozenset({1, 2}))

    def test_staggered_expiry_shrinks_then_drops_group(self):
        c = LacpController(clock=ManualClock())
        c.port_up(1)
        c.port_up(2)
        c.receive_advertisement(1, "A", 100, True, ttl=5)
        c.receive_advertisement(2, "A", 100, True, ttl=20)

        c.tick(6)
        # Port 1 expired but port 2 keeps the group alive.
        self.assertEqual(c.selected_group, ("A", 100))
        self.assertFalse(c.can_collect(1))
        self.assertTrue(c.can_collect(2))

        c.tick(20)
        self.assertIsNone(c.selected_group)
        self.assertEqual(c.collecting_ports(), frozenset())

    def test_port_down_stops_collection(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2])
        c.port_down(1)
        self.assertEqual(c.collecting_ports(), frozenset({2}))
        state = c.port_state(1)
        self.assertFalse(state.up)
        self.assertIsNone(state.neighbor)

        c.port_up(1)
        # Coming back up without an advertisement does not restore collection.
        self.assertFalse(c.can_collect(1))
        c.receive_advertisement(1, "A", 100, True)
        self.assertTrue(c.can_collect(1))

    def test_advertisement_on_down_port_is_ignored(self):
        c = LacpController(clock=ManualClock())
        c.port_up(1)
        c.port_down(1)
        c.receive_advertisement(1, "A", 100, True)
        self.assertIn(
            ("advertisement_down_or_unknown", 1), c.ignored_events
        )
        self.assertFalse(c.can_collect(1))

    def test_refreshed_advertisement_prevents_timeout(self):
        c = LacpController(ttl=10.0, clock=ManualClock())
        bring_group(c, [1], ttl=10.0)
        c.tick(8)
        c.receive_advertisement(1, "A", 100, True, ttl=10.0)
        c.tick(9)
        self.assertTrue(c.can_collect(1))
        c.tick(2)  # now past the refreshed deadline
        self.assertFalse(c.can_collect(1))

    def test_whole_group_collapse_arms_drain_and_blocks_on_return(self):
        c = LacpController(ttl=10.0, clock=ManualClock())
        bring_group(c, [1, 2], ttl=10.0)
        c.send_frame(1)
        c.tick(11)  # every advertisement expires together
        self.assertIsNone(c.selected_group)
        self.assertEqual(c.collecting_ports(), frozenset())
        self.assertEqual(
            set().union(*c.pending_drains().values()), {1}
        )

        # The group comes back: collection resumes immediately, but
        # distribution stays blocked until the old frame is proven drained.
        bring_group(c, [1, 2], ttl=10.0)
        self.assertEqual(c.collecting_ports(), frozenset({1, 2}))
        self.assertEqual(c.distributing_ports(), frozenset())
        c.receive_marker_response(1, c.generation)
        self.assertEqual(c.distributing_ports(), frozenset({1, 2}))

    def test_unsynced_flag_toggle_drives_collection(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1])
        self.assertTrue(c.can_collect(1))
        c.receive_advertisement(1, "A", 100, False)
        self.assertFalse(c.can_collect(1))
        c.receive_advertisement(1, "A", 100, True)
        self.assertTrue(c.can_collect(1))

    def test_stale_advertisement_pauses_member_without_drain(self):
        # A member whose advertisement goes stale keeps its group membership:
        # it temporarily stops collecting/distributing, but its in-flight
        # frames do NOT require a marker response, and it resumes cleanly
        # once a fresh advertisement arrives.
        c = LacpController(ttl=50.0, clock=ManualClock())
        bring_group(c, [1, 2], ttl=50.0)
        self.assertTrue(c.send_frame(1))
        c.receive_advertisement(1, "A", 100, True, ttl=5)
        c.tick(6)

        self.assertEqual(c.selected_group, ("A", 100))
        self.assertTrue(c.port_state(1).in_selected_group)
        self.assertFalse(c.can_collect(1))
        self.assertFalse(c.can_distribute(1))
        self.assertEqual(c.pending_drains(), {})  # no drain armed
        # The healthy member keeps the group serving traffic.
        self.assertTrue(c.can_collect(2))
        self.assertTrue(c.can_distribute(2))
        self.assertEqual(c.port_state(1).outstanding_frames, 1)

        c.receive_advertisement(1, "A", 100, True, ttl=50.0)
        self.assertTrue(c.can_collect(1))
        self.assertTrue(c.can_distribute(1))
        self.assertEqual(c.pending_drains(), {})


if __name__ == "__main__":
    unittest.main()
