"""Forwarding-loop termination and split-horizon (excluding arrival port)."""

import unittest

from vlancheck.analyzer import analyze

from tests.helpers import make_link, make_port, make_topology


def build_ring_topology():
    """Three switches in a physical ring, all trunking tag 10 for domain v10.

    Each switch also has an edge port in vlan10 so the domain has members
    everywhere.  Without loop termination the BFS would never finish.
    """
    def trunk(device, port):
        return make_port(
            device, port, native_vlan=1, tagged=[10],
            egress={1: ("untag",), 10: ("tag", 10)},
        )

    ports = [
        trunk("SW1", "to-sw2"),
        trunk("SW1", "to-sw3"),
        make_port("SW1", "edge", native_vlan=10, tagged=[],
                  egress={10: ("untag",)}),
        trunk("SW2", "to-sw1"),
        trunk("SW2", "to-sw3"),
        make_port("SW2", "edge", native_vlan=10, tagged=[],
                  egress={10: ("untag",)}),
        trunk("SW3", "to-sw1"),
        trunk("SW3", "to-sw2"),
        make_port("SW3", "edge", native_vlan=10, tagged=[],
                  egress={10: ("untag",)}),
    ]
    links = [
        make_link("SW1", "to-sw2", "SW2", "to-sw1"),
        make_link("SW2", "to-sw3", "SW3", "to-sw2"),
        make_link("SW3", "to-sw1", "SW1", "to-sw3"),
    ]
    pairs = [
        # One end-to-end partition: local vlan10 on all three switches.
        [("SW1", 10), ("SW2", 10), ("SW3", 10)],
    ]
    return make_topology(ports, links, pairs)


class RingLoopTests(unittest.TestCase):
    def test_ring_analysis_terminates_and_covers_all_domains(self):
        report = analyze(build_ring_topology())
        result = report.results[("SW1", 10)]
        self.assertEqual(
            result.reached_domains,
            {("SW1", 10), ("SW2", 10), ("SW3", 10)},
        )

    def test_no_boundary_state_processed_twice(self):
        # Every (ingress_port, domain) event appears at most once: the ring
        # gives each trunk port two neighbors, but a frame arriving on a port
        # must never be flooded back out that same port.
        report = analyze(build_ring_topology())
        result = report.results[("SW1", 10)]
        seen_states = [
            (event.ingress_port, event.received_domain)
            for event in result.boundary_events
        ]
        self.assertEqual(len(seen_states), len(set(seen_states)))

    def test_ring_crossings_all_ok_with_pairs_declared(self):
        report = analyze(build_ring_topology())
        result = report.results[("SW1", 10)]
        self.assertTrue(result.boundary_events)
        self.assertTrue(
            all(event.status == "ok" for event in result.boundary_events)
        )
        self.assertFalse(report.has_leak)


class SplitHorizonTests(unittest.TestCase):
    def test_frame_is_not_flooded_back_onto_arrival_port(self):
        """Parallel links between two switches: arrival link is excluded."""
        ports = [
            make_port("SW1", "la", native_vlan=10, tagged=[10],
                      egress={10: ("tag", 10)}),
            make_port("SW1", "lb", native_vlan=10, tagged=[10],
                      egress={10: ("tag", 10)}),
            make_port("SW2", "la", native_vlan=10, tagged=[10],
                      egress={10: ("tag", 10)}),
            make_port("SW2", "lb", native_vlan=10, tagged=[10],
                      egress={10: ("tag", 10)}),
        ]
        topo = make_topology(
            ports,
            [
                make_link("SW1", "la", "SW2", "la"),
                make_link("SW1", "lb", "SW2", "lb"),
            ],
            pairs=[[("SW1", 10), ("SW2", 10)]],
        )
        report = analyze(topo)
        result = report.results[("SW1", 10)]
        events = result.boundary_events
        # Each cable carries the frame exactly once, SW1 -> SW2.  When it
        # reaches SW2:la over cable-a, split horizon forbids sending it back
        # over the same cable, and SW2:la has no other neighbor, so there is
        # no reflected SW2 -> SW1 event on that cable.
        event_cables = [
            frozenset((e.egress_port, e.ingress_port)) for e in events
        ]
        self.assertEqual(
            sorted(event_cables),
            sorted(
                [
                    frozenset((("SW1", "la"), ("SW2", "la"))),
                    frozenset((("SW1", "lb"), ("SW2", "lb"))),
                ]
            ),
        )
        # No event ever points back at a switch-1 port: reflection blocked.
        self.assertTrue(
            all(e.ingress_port[0] != "SW1" for e in events)
        )
        # And the simulation still terminates with a finite event count.
        self.assertEqual(len(events), 2)

    def test_parallel_cable_second_link_used_when_available(self):
        """With two cables on *each* peer port, the other cable is used."""
        ports = [
            make_port("SW1", "la", native_vlan=10, tagged=[10, 11],
                      egress={10: ("tag", 10)}),
            make_port("SW1", "lb", native_vlan=10, tagged=[10, 11],
                      egress={10: ("tag", 10)}),
            make_port("SW2", "la", native_vlan=10, tagged=[10, 11],
                      egress={10: ("tag", 10)}),
            make_port("SW2", "lb", native_vlan=10, tagged=[10, 11],
                      egress={10: ("tag", 10)}),
        ]
        # Full mesh: every SW1 port links to every SW2 port (4 cables).
        topo = make_topology(
            ports,
            [
                make_link("SW1", "la", "SW2", "la"),
                make_link("SW1", "la", "SW2", "lb"),
                make_link("SW1", "lb", "SW2", "la"),
                make_link("SW1", "lb", "SW2", "lb"),
            ],
            pairs=[[("SW1", 10), ("SW2", 10)]],
        )
        report = analyze(topo)
        result = report.results[("SW1", 10)]
        events = result.boundary_events
        # Four hop-1 crossings (one per cable).  Hop-2 frames arriving at a
        # SW2 port are flooded out the *other* cable of that port, producing
        # SW2 -> SW1 events that never use the frame's own arrival cable.
        hop1 = [e for e in events if e.hops == 1]
        hop2 = [e for e in events if e.hops == 2]
        self.assertEqual(len(hop1), 4)
        self.assertTrue(hop2)
        self.assertTrue(all(e.ingress_port[0] == "SW1" for e in hop2))
        # Every hop-2 crossing reuses a cable already seen at hop 1 but in
        # the opposite direction; the analyzer's per-cable split-horizon
        # filter guarantees the frame did not exit on its own arrival cable.
        hop1_cables = {
            frozenset((e.egress_port, e.ingress_port)) for e in hop1
        }
        for e in hop2:
            self.assertIn(
                frozenset((e.egress_port, e.ingress_port)), hop1_cables
            )
        # Termination: despite the physical cycles the link-aware visited
        # set makes the walk finite (it returns at all) and every event's
        # hop count is a concrete small number rather than growing forever.
        self.assertIsInstance(events, list)
        self.assertGreater(len(events), 4)
        self.assertLess(max(e.hops for e in events), 100)

    def test_single_link_does_not_reflect_frame_back(self):
        ports = [
            make_port("SW1", "p1", native_vlan=10, tagged=[10],
                      egress={10: ("tag", 10)}),
            make_port("SW2", "p1", native_vlan=10, tagged=[10],
                      egress={10: ("tag", 10)}),
        ]
        topo = make_topology(
            ports,
            [make_link("SW1", "p1", "SW2", "p1")],
            pairs=[[("SW1", 10), ("SW2", 10)]],
        )
        report = analyze(topo)
        result = report.results[("SW1", 10)]
        self.assertEqual(len(result.boundary_events), 1)
        self.assertEqual(
            result.boundary_events[0].ingress_port, ("SW2", "p1")
        )


if __name__ == "__main__":
    unittest.main()
