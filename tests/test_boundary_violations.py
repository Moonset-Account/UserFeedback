"""Boundary violation cases.

Covers the headline problem: the *same* numeric VLAN id on two adjacent
switches names two different isolation domains.  Also covers stripping a
tag and distinguishing a correct untagged reception from absorption into
the peer's native/default domain.
"""

import unittest

from vlancheck.analyzer import analyze

from tests.helpers import make_link, make_port, make_topology


def build_same_number_different_domain_topology():
    """VLAN 10 means RED on SW1 but BLUE on SW2; trunk carries tag 10.

    RED and BLUE are each paired with their real counterparts elsewhere,
    so the SW1:10 -> SW2:10 crossing is a genuine leak with a prescribed
    pair to check against.
    """
    ports = [
        # SW1 side: vlan10 = RED
        make_port("SW1", "trunk", native_vlan=1, tagged=[10],
                  egress={1: ("untag",), 10: ("tag", 10)}),
        make_port("SW1", "red-edge", native_vlan=10, tagged=[],
                  egress={10: ("untag",)}),
        # SW2 side: vlan10 = BLUE (same number, different partition)
        make_port("SW2", "trunk", native_vlan=1, tagged=[10],
                  egress={1: ("untag",), 10: ("tag", 10)}),
        make_port("SW2", "blue-edge", native_vlan=10, tagged=[],
                  egress={10: ("untag",)}),
        # SW3 hosts the real RED peer with another numeric id.
        make_port("SW3", "to-sw1", native_vlan=1, tagged=[10],
                  egress={1: ("untag",), 30: ("tag", 10)}),
        make_port("SW3", "red-edge", native_vlan=30, tagged=[],
                  egress={30: ("untag",)}),
        make_port("SW1", "to-sw3", native_vlan=1, tagged=[10],
                  egress={1: ("untag",), 10: ("tag", 10)}),
    ]
    links = [
        make_link("SW1", "trunk", "SW2", "trunk"),
        make_link("SW1", "to-sw3", "SW3", "to-sw1"),
    ]
    pairs = [
        # RED: SW1 vlan10 is really the same domain as SW3 vlan30.
        [("SW1", 10), ("SW3", 30)],
        # BLUE is prescribed entirely on SW2 side expectations.
    ]
    return make_topology(ports, links, pairs)


def build_untagged_topology(peer_native_vlan, peer_members):
    """SW1 strips vlan20 on egress; SW2 receives on its native VLAN.

    ``peer_members`` lists the local vlans SW2's receiving port carries,
    which lets a test prescribe a pair even when the native domain differs.
    """
    ports = [
        make_port("SW1", "p1", native_vlan=20, tagged=[],
                  egress={20: ("untag",)}),
        make_port("SW1", "edge", native_vlan=20, tagged=[],
                  egress={20: ("untag",)}),
        make_port(
            "SW2", "p1", native_vlan=peer_native_vlan, tagged=[],
            egress={vid: ("untag",) for vid in peer_members},
        ),
    ]
    links = [make_link("SW1", "p1", "SW2", "p1")]
    pairs = [[("SW1", 20), ("SW2", 20)]]
    return make_topology(ports, links, pairs)


class SameVlanNumberDifferentDomainTests(unittest.TestCase):
    def setUp(self):
        self.report = analyze(build_same_number_different_domain_topology())

    def test_same_number_crossing_is_leak_with_first_receiver(self):
        result = self.report.results[("SW1", 10)]
        self.assertIn(("SW2", 10), result.reached_domains)
        self.assertIsNotNone(result.first_leak)
        leak = result.first_leak
        self.assertEqual(leak.ingress_port, ("SW2", "trunk"))
        self.assertEqual(leak.received_domain, ("SW2", 10))
        self.assertEqual(leak.status, "leak")
        # The prescribed pair said SW3:30 was the expected peer.
        self.assertEqual(leak.expected_domain, ("SW3", 30))

    def test_first_leak_is_earliest_hop_then_deterministic_port_order(self):
        result = self.report.results[("SW1", 10)]
        for event in result.boundary_events:
            if event.is_leak:
                # first_leak must be the earliest leak in event order.
                self.assertEqual(event, result.first_leak)
                break
        self.assertEqual(result.first_leak.hops, 1)

    def test_correctly_paired_domain_elsewhere_is_ok(self):
        # SW3:30 is reached via the second link and must classify ok.
        result = self.report.results[("SW1", 10)]
        ok_events = [e for e in result.boundary_events if e.status == "ok"]
        self.assertTrue(
            any(e.received_domain == ("SW3", 30) for e in ok_events)
        )

    def test_report_surfaces_leaking_root(self):
        self.assertTrue(self.report.has_leak)
        self.assertIn(("SW1", 10), self.report.leaking_roots)
        self.assertEqual(
            self.report.leaking_roots[("SW1", 10)].received_domain,
            ("SW2", 10),
        )


class UntaggedReceptionTests(unittest.TestCase):
    def test_stripped_frame_correctly_received_in_matching_native(self):
        report = analyze(
            build_untagged_topology(peer_native_vlan=20, peer_members=[20])
        )
        result = report.results[("SW1", 20)]
        event = result.boundary_events[0]
        self.assertIsNone(event.sent_tag)
        self.assertEqual(event.received_domain, ("SW2", 20))
        self.assertEqual(event.status, "untagged_ok")
        self.assertIsNone(result.first_leak)
        self.assertEqual(result.stripped_and_correct, [event])
        self.assertEqual(result.stripped_but_misreceived, [])

    def test_stripped_frame_misreceived_by_foreign_default_domain(self):
        report = analyze(
            build_untagged_topology(peer_native_vlan=5, peer_members=[5, 20])
        )
        result = report.results[("SW1", 20)]
        event = result.boundary_events[0]
        self.assertIsNone(event.sent_tag)
        self.assertEqual(event.received_domain, ("SW2", 5))
        self.assertEqual(event.status, "untagged_native_misreceive")
        self.assertIsNotNone(result.first_leak)
        self.assertEqual(result.first_leak, event)
        self.assertEqual(result.stripped_but_misreceived, [event])

    def test_two_statuses_are_distinguishable_in_one_report(self):
        """Two parallel receivers: one correct native, one foreign native."""
        ports = [
            make_port("SW1", "p1", native_vlan=20, tagged=[],
                      egress={20: ("untag",)}),
            make_port("SW1", "p2", native_vlan=20, tagged=[],
                      egress={20: ("untag",)}),
            make_port("SW1", "edge", native_vlan=20, tagged=[],
                      egress={20: ("untag",)}),
            make_port("SW2", "p1", native_vlan=20, tagged=[],
                      egress={20: ("untag",)}),
            make_port("SW3", "p1", native_vlan=5, tagged=[],
                      egress={5: ("untag",), 20: ("untag",)}),
        ]
        topo = make_topology(
            ports,
            [
                make_link("SW1", "p1", "SW2", "p1"),
                make_link("SW1", "p2", "SW3", "p1"),
            ],
            pairs=[[("SW1", 20), ("SW2", 20)]],
        )
        report = analyze(topo)
        result = report.results[("SW1", 20)]
        statuses = {e.received_domain: e.status for e in result.boundary_events}
        self.assertEqual(statuses[("SW2", 20)], "untagged_ok")
        self.assertEqual(statuses[("SW3", 5)], "untagged_native_misreceive")
        self.assertTrue(report.has_leak)


if __name__ == "__main__":
    unittest.main()
