import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from lacp import LacpController, ManualClock, PortState


def bring_group(controller, ports, system_id="A", key=100, ttl=30.0, synced=True):
    for pid in ports:
        controller.port_up(pid)
        controller.receive_advertisement(pid, system_id, key, synced, ttl=ttl)


class InterleavingTest(unittest.TestCase):
    def test_drain_response_interleaved_with_new_advertisements(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2], "A", 100)
        c.send_frame(1)
        c.send_frame(2)
        # Equal-size challenger with lower identifier wins the first switch.
        bring_group(c, [3, 4], "A", 99)
        first_switch_gen = c.generation

        # A larger challenger appears while the old drain is still pending;
        # selection changes again and the pending drains are re-armed with
        # the new generation.
        bring_group(c, [5, 6, 7], "A", 98)
        second_switch_gen = c.generation
        self.assertEqual(c.selected_group, ("A", 98))
        self.assertEqual(c.distributing_ports(), frozenset())
        self.assertEqual(set().union(*c.pending_drains().values()), {1, 2})

        # Responses tagged with the first switch's generation are now late.
        c.receive_marker_response(1, first_switch_gen)
        c.receive_marker_response(2, first_switch_gen)
        self.assertEqual(len(c.stale_responses), 2)
        self.assertEqual(c.distributing_ports(), frozenset())

        # Correct (current-generation) responses, interleaved with an
        # unrelated advertisement that grows the selected group.
        c.receive_marker_response(1, second_switch_gen)
        c.port_up(8)
        c.receive_advertisement(8, "A", 98, True)
        self.assertEqual(c.collecting_ports(), frozenset({5, 6, 7, 8}))
        self.assertEqual(c.distributing_ports(), frozenset())
        c.receive_marker_response(2, second_switch_gen)
        self.assertEqual(
            c.distributing_ports(), frozenset({5, 6, 7, 8})
        )

    def test_down_during_pending_drain_releases_only_that_member(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2, 3], "A", 100)
        c.send_frame(1)
        c.send_frame(2)
        bring_group(c, [4, 5, 6, 7], "B", 200)
        self.assertEqual(set().union(*c.pending_drains().values()), {1, 2})

        c.port_down(1)
        self.assertEqual(set().union(*c.pending_drains().values()), {2})
        self.assertEqual(c.distributing_ports(), frozenset())
        c.receive_marker_response(2, c.generation)
        self.assertEqual(c.distributing_ports(), frozenset({4, 5, 6, 7}))

    def test_flapping_port_rejoins_without_phantom_drain(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2], "A", 100)
        c.send_frame(1)
        c.port_down(1)  # down link releases its drain immediately
        self.assertEqual(c.pending_drains(), {})
        self.assertEqual(c.distributing_ports(), frozenset({2}))

        bring_group(c, [1], "A", 100)  # flap back, same group
        self.assertEqual(c.selected_group, ("A", 100))
        self.assertEqual(c.distributing_ports(), frozenset({1, 2}))
        self.assertEqual(c.pending_drains(), {})

    def test_string_and_integer_identifiers_coexist(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, ["p1", "p2"], "sys-A", "key-x")
        bring_group(c, [10, 11, 12], "sys-B", 7)
        self.assertEqual(c.selected_group, ("sys-B", 7))
        self.assertEqual(c.collecting_ports(), frozenset({10, 11, 12}))
        self.assertTrue(c.send_frame(10))
        self.assertFalse(c.send_frame("p1"))

    def test_port_state_snapshot_fields(self):
        c = LacpController(ttl=5.0, clock=ManualClock())
        bring_group(c, [1], ttl=5.0)
        c.send_frame(1)
        snap = c.port_state(1)
        self.assertIsInstance(snap, PortState)
        self.assertTrue(snap.up)
        self.assertTrue(snap.in_selected_group)
        self.assertTrue(snap.collecting)
        self.assertTrue(snap.distributing)
        self.assertEqual(snap.outstanding_frames, 1)
        self.assertEqual(snap.neighbor.system_id, "A")
        self.assertEqual(snap.neighbor.key, 100)

        c.tick(6)
        snap = c.port_state(1)
        self.assertFalse(snap.collecting)
        self.assertFalse(snap.distributing)
        self.assertEqual(snap.outstanding_frames, 1)

    def test_unknown_port_down_recorded_not_raised(self):
        c = LacpController(clock=ManualClock())
        c.port_down(42)
        self.assertEqual(c.ignored_events, [("port_down_unknown", 42)])

    def test_invalid_ttl_rejected(self):
        c = LacpController(clock=ManualClock())
        with self.assertRaises(ValueError):
            LacpController(ttl=0)
        c.port_up(1)
        with self.assertRaises(ValueError):
            c.receive_advertisement(1, "A", 1, True, ttl=0)

    def test_generation_increments_only_on_identity_change(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2], "A", 100)
        self.assertEqual(c.generation, 1)  # initial selection opens gen 1
        # Repeated identical advertisements change nothing.
        c.receive_advertisement(1, "A", 100, True)
        c.receive_advertisement(2, "A", 100, True)
        self.assertEqual(c.generation, 1)
        # Membership growth within the same group keeps the generation.
        bring_group(c, [3], "A", 100)
        self.assertEqual(c.generation, 1)
        # Identity change bumps it (the challenger must be large enough to
        # win the selection against the three-member incumbent).
        bring_group(c, [4, 5, 6, 7], "B", 200)
        self.assertEqual(c.generation, 2)
        # Collapse to no candidate group opens another generation; every
        # member of the selected group must leave.
        for pid in (1, 2, 3, 4, 5, 6, 7):
            c.port_down(pid)
        self.assertIsNone(c.selected_group)
        self.assertEqual(c.generation, 3)


if __name__ == "__main__":
    unittest.main()
