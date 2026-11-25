import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from lacp import LacpController, ManualClock, MarkerRequest


def bring_group(controller, ports, system_id="A", key=100, ttl=30.0, synced=True):
    for pid in ports:
        controller.port_up(pid)
        controller.receive_advertisement(pid, system_id, key, synced, ttl=ttl)


class DistributionDrainTest(unittest.TestCase):
    def test_syncable_members_distribute_immediately_without_pending(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2])
        self.assertEqual(c.distributing_ports(), frozenset({1, 2}))
        self.assertTrue(c.send_frame(1))
        self.assertEqual(c.port_state(1).outstanding_frames, 1)

    def test_switch_blocks_new_group_until_all_old_members_drain(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2], "A", 100)
        self.assertTrue(c.send_frame(1))
        self.assertTrue(c.send_frame(2))
        gen_a = c.generation

        # A larger challenger wins the selection.
        bring_group(c, [3, 4, 5], "B", 200)
        self.assertEqual(c.generation, gen_a + 1)
        # Old members stopped, new members may collect but not distribute.
        self.assertEqual(c.distributing_ports(), frozenset())
        self.assertEqual(c.collecting_ports(), frozenset({3, 4, 5}))
        self.assertFalse(c.send_frame(3))
        # Marker requests were emitted for the two members with traffic.
        self.assertEqual(
            sorted((r.port_id, r.generation) for r in c.marker_requests),
            [(1, c.generation), (2, c.generation)],
        )
        pending = c.pending_drains()
        self.assertEqual(set().union(*pending.values()), {1, 2})

        # One marker response is not enough: the other member still blocks.
        c.receive_marker_response(1, c.generation)
        self.assertEqual(c.distributing_ports(), frozenset())
        self.assertEqual(set().union(*c.pending_drains().values()), {2})

        c.receive_marker_response(2, c.generation)
        self.assertEqual(c.pending_drains(), {})
        self.assertEqual(c.distributing_ports(), frozenset({3, 4, 5}))
        self.assertTrue(c.send_frame(3))

    def test_switch_without_traffic_never_blocks(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1, 2], "A", 100)
        bring_group(c, [3, 4, 5], "B", 200)
        # No frames were sent, so nothing needs draining.
        self.assertEqual(c.pending_drains(), {})
        self.assertEqual(c.distributing_ports(), frozenset({3, 4, 5}))
        self.assertEqual(c.marker_requests, [])

    def test_late_old_generation_marker_response_releases_nothing(self):
        c = LacpController(clock=ManualClock())
        # Incumbent group A/100 with one traffic-bearing member.
        bring_group(c, [1], "A", 100)
        gen1 = c.generation  # initial selection opens generation 1
        self.assertTrue(c.send_frame(1))

        # A smaller second group exists but cannot win yet.
        bring_group(c, [2], "A", 99)
        self.assertEqual(c.selected_group, ("A", 100))

        # Growing the challenger to equal size forces a tie-break switch in
        # one reconciliation; the incumbent is not preferred because sizes
        # only become equal as the challenger's new member arrives... the
        # incumbent rule still applies here, so instead shrink the incumbent
        # by timing its member out while the challenger stays synced.
        c.receive_advertisement(1, "A", 100, True, ttl=5)
        c.receive_advertisement(2, "A", 99, True, ttl=50)
        c.tick(6)
        gen2 = c.generation
        self.assertEqual(gen2, gen1 + 1)
        self.assertEqual(c.selected_group, ("A", 99))
        self.assertFalse(c.can_distribute(2))

        # A response tagged with the pre-switch generation is late.
        c.receive_marker_response(1, gen1)
        self.assertEqual(c.stale_responses, [(1, gen1)])
        self.assertFalse(c.can_distribute(2))
        self.assertEqual(set().union(*c.pending_drains().values()), {1})

        # The correctly-tagged response releases the drain.
        c.receive_marker_response(1, gen2)
        self.assertEqual(c.stale_responses, [(1, gen1)])
        self.assertTrue(c.can_distribute(2))

    def test_late_response_survives_two_switches(self):
        c = LacpController(ttl=50.0, clock=ManualClock())
        bring_group(c, [1], "A", 100, ttl=50.0)
        self.assertTrue(c.send_frame(1))

        # First switch: the incumbent's advertisement expires while a
        # lower-identifier group stays synchronized.
        bring_group(c, [2], "A", 99, ttl=50.0)
        c.receive_advertisement(1, "A", 100, True, ttl=5)
        c.tick(6)
        gen_switch_1 = c.generation
        self.assertEqual(c.selected_group, ("A", 99))
        self.assertFalse(c.can_distribute(2))

        # Second switch before the first drain completes: the current group's
        # member expires while an even lower-identifier group stays synced.
        bring_group(c, [3], "A", 98, ttl=50.0)
        c.receive_advertisement(2, "A", 99, True, ttl=5)
        c.tick(6)
        gen_switch_2 = c.generation
        self.assertGreater(gen_switch_2, gen_switch_1)
        self.assertEqual(c.selected_group, ("A", 98))

        # Port 1's drain was re-armed at the second switch: the response for
        # the first switch's generation is now stale.
        c.receive_marker_response(1, gen_switch_1)
        self.assertIn((1, gen_switch_1), c.stale_responses)
        self.assertFalse(c.can_distribute(3))

        c.receive_marker_response(1, gen_switch_2)
        self.assertTrue(c.can_distribute(3))

    def test_advertisement_timeout_is_not_drain_proof(self):
        c = LacpController(ttl=50.0, clock=ManualClock())
        bring_group(c, [1], "A", 100, ttl=50.0)
        self.assertTrue(c.send_frame(1))

        # A second group exists but is smaller, so the incumbent stays.
        bring_group(c, [2], "A", 99, ttl=50.0)
        self.assertEqual(c.selected_group, ("A", 100))

        # The incumbent's advertisement gets a short lifetime and then
        # expires: selection switches to group A/99, opening a new
        # generation and arming port 1's drain.
        c.receive_advertisement(1, "A", 100, True, ttl=5)
        c.tick(6)
        self.assertEqual(c.selected_group, ("A", 99))
        self.assertFalse(c.can_collect(1))
        self.assertFalse(c.can_distribute(2))
        self.assertEqual(set().union(*c.pending_drains().values()), {1})

        # Expiry alone never proves the old frame drained: more time passing
        # (while the new group is still fresh) changes nothing.
        c.tick(10)
        self.assertTrue(c.can_collect(2))
        self.assertEqual(set().union(*c.pending_drains().values()), {1})
        self.assertFalse(c.can_distribute(2))

        # A marker response with the current generation finally unblocks.
        current_gen = c.generation
        c.receive_marker_response(1, current_gen)
        self.assertTrue(c.can_distribute(2))

    def test_link_down_is_accepted_as_drain_proof(self):
        c = LacpController(ttl=50.0, clock=ManualClock())
        bring_group(c, [1], "A", 100, ttl=50.0)
        self.assertTrue(c.send_frame(1))
        bring_group(c, [2], "A", 99, ttl=50.0)
        # Force the switch via a short-lived incumbent advertisement.
        c.receive_advertisement(1, "A", 100, True, ttl=5)
        c.tick(6)
        self.assertEqual(c.selected_group, ("A", 99))
        self.assertFalse(c.can_distribute(2))

        c.port_down(1)  # link really gone: no frame can remain in flight.
        self.assertEqual(c.pending_drains(), {})
        self.assertTrue(c.can_distribute(2))
        self.assertTrue(c.send_frame(2))

    def test_member_loss_within_same_group_keeps_identity(self):
        c = LacpController(ttl=10.0, clock=ManualClock())
        bring_group(c, [1, 2], "A", 100, ttl=10.0)
        self.assertTrue(c.send_frame(1))
        gen_before = c.generation

        c.port_down(1)
        # Group identity unchanged, so no new generation; port 2 keeps
        # distributing and port 1's down link proved its own drain.
        self.assertEqual(c.selected_group, ("A", 100))
        self.assertEqual(c.generation, gen_before)
        self.assertEqual(c.distributing_ports(), frozenset({2}))
        self.assertEqual(c.pending_drains(), {})

    def test_marker_response_for_unknown_port_is_stale(self):
        c = LacpController(clock=ManualClock())
        bring_group(c, [1], "A", 100)
        c.receive_marker_response(99, 1)
        self.assertEqual(c.stale_responses, [(99, 1)])

    def test_marker_request_carries_generation_and_timestamp(self):
        c = LacpController(ttl=50.0, clock=ManualClock())
        bring_group(c, [1], "A", 100, ttl=50.0)
        c.send_frame(1)
        # A smaller challenger waits in the wings; expiring the incumbent's
        # short-lived advertisement then forces the switch.
        bring_group(c, [2], "A", 99, ttl=50.0)
        c.receive_advertisement(1, "A", 100, True, ttl=5)
        c.clock.advance(6)  # t=6, past the t=5 advertisement deadline
        c.tick(0)

        (req,) = c.marker_requests
        self.assertIsInstance(req, MarkerRequest)
        self.assertEqual(req.port_id, 1)
        self.assertEqual(req.generation, c.generation)
        self.assertEqual(req.sent_at, 6.0)

    def test_stale_member_moving_to_other_group_must_drain(self):
        # A stale (desynchronized) member keeps its membership, but once it
        # advertises a different group it truly leaves and its outstanding
        # frames require drain proof.
        c = LacpController(ttl=50.0, clock=ManualClock())
        bring_group(c, [1, 2], "A", 100, ttl=50.0)
        c.send_frame(1)
        c.receive_advertisement(1, "A", 100, True, ttl=5)
        c.tick(6)  # p1 stale, still a member, no drain yet
        self.assertTrue(c.port_state(1).in_selected_group)
        self.assertEqual(c.pending_drains(), {})

        c.receive_advertisement(1, "B", 200, True, ttl=50.0)
        self.assertFalse(c.port_state(1).in_selected_group)
        self.assertEqual(
            set().union(*c.pending_drains().values()), {1}
        )
        # While the drain is pending, no member distributes, including the
        # healthy member that stays in the selected group.
        self.assertEqual(c.distributing_ports(), frozenset())
        self.assertTrue(c.can_collect(2))

        c.receive_marker_response(1, c.generation)
        self.assertEqual(c.pending_drains(), {})
        self.assertEqual(c.distributing_ports(), frozenset({2}))

    def test_send_frame_rejected_when_not_distributing(self):
        c = LacpController(clock=ManualClock())
        c.port_up(1)  # up but never synchronized
        self.assertFalse(c.send_frame(1))
        self.assertFalse(c.send_frame(99))

    def test_distribution_blocked_when_own_advertisement_expires(self):
        c = LacpController(ttl=10.0, clock=ManualClock())
        bring_group(c, [1, 2], ttl=10.0)
        self.assertTrue(c.send_frame(1))
        c.tick(11)
        # Whole group gone; the outstanding frame needs a marker response and
        # nothing distributes.
        self.assertEqual(c.distributing_ports(), frozenset())
        self.assertEqual(set().union(*c.pending_drains().values()), {1})

        # Re-advertise: collection can return, but distribution stays blocked
        # until the drain proof arrives.
        bring_group(c, [1, 2], ttl=10.0)
        self.assertEqual(c.collecting_ports(), frozenset({1, 2}))
        self.assertEqual(c.distributing_ports(), frozenset())
        c.receive_marker_response(1, c.generation)
        self.assertEqual(c.distributing_ports(), frozenset({1, 2}))


if __name__ == "__main__":
    unittest.main()
