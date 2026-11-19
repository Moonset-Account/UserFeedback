"""Acceptable-result cases: correct tagged forwarding and bidirectional rewrite."""

import unittest

from vlancheck.analyzer import analyze

from tests.helpers import make_link, make_port, make_topology


def build_two_switch_topology():
    """Clean two-switch topology with two prescribed domain pairs.

    Pair A: SW1 vlan10  <-> SW2 vlan110 (different numeric ids; both sides
            egress/ingress wire tag 110, i.e. tag translation on SW1).
    Pair B: SW1 vlan20  <-> SW2 vlan20  (same numeric id, plain trunk tag).
    Native vlan99 on both trunk ports is paired too and crosses untagged.
    """
    ports = [
        make_port(
            "SW1", "p1", native_vlan=99, tagged=[110, 20],
            egress={99: ("untag",), 10: ("tag", 110), 20: ("tag", 20)},
        ),
        make_port(
            "SW2", "p1", native_vlan=99, tagged=[110, 20],
            egress={99: ("untag",), 110: ("tag", 110), 20: ("tag", 20)},
        ),
        # Edge access ports so every domain has a second member.
        make_port("SW1", "acc", native_vlan=10, tagged=[], egress={10: ("untag",)}),
        make_port("SW2", "acc", native_vlan=110, tagged=[], egress={110: ("untag",)}),
        make_port("SW1", "acc20", native_vlan=20, tagged=[], egress={20: ("untag",)}),
        make_port("SW2", "acc20", native_vlan=20, tagged=[], egress={20: ("untag",)}),
    ]
    links = [make_link("SW1", "p1", "SW2", "p1")]
    pairs = [
        [("SW1", 10), ("SW2", 110)],
        [("SW1", 20), ("SW2", 20)],
        [("SW1", 99), ("SW2", 99)],
    ]
    return make_topology(ports, links, pairs)


class AcceptableForwardingTests(unittest.TestCase):
    def setUp(self):
        self.report = analyze(build_two_switch_topology())

    def test_rewritten_tag_reaches_paired_domain(self):
        result = self.report.results[("SW1", 10)]
        self.assertIn(("SW2", 110), result.reached_domains)
        crossing = next(
            event
            for event in result.boundary_events
            if event.ingress_port == ("SW2", "p1")
        )
        self.assertEqual(crossing.sent_tag, 110)
        self.assertEqual(crossing.received_domain, ("SW2", 110))
        self.assertEqual(crossing.status, "ok")
        self.assertIsNone(result.first_leak)

    def test_same_number_tag_reaches_matching_domain(self):
        result = self.report.results[("SW1", 20)]
        self.assertIn(("SW2", 20), result.reached_domains)
        self.assertTrue(
            all(not event.is_leak for event in result.boundary_events)
        )

    def test_reverse_direction_of_translation_is_ok(self):
        result = self.report.results[("SW2", 110)]
        crossing = next(
            event
            for event in result.boundary_events
            if event.ingress_port == ("SW1", "p1")
        )
        # Same wire label in both directions; SW1 maps tag 110 to local 10.
        self.assertEqual(crossing.sent_tag, 110)
        self.assertEqual(crossing.received_domain, ("SW1", 10))
        self.assertEqual(crossing.status, "ok")

    def test_native_pair_crosses_untagged_ok(self):
        result = self.report.results[("SW1", 99)]
        crossing = result.boundary_events[0]
        self.assertIsNone(crossing.sent_tag)
        self.assertEqual(crossing.status, "untagged_ok")

    def test_clean_topology_has_no_drops_and_no_leaks(self):
        self.assertEqual(self.report.drops, [])
        self.assertFalse(self.report.has_leak)
        self.assertEqual(self.report.leaking_roots, {})

    def test_roots_cover_every_local_domain(self):
        expected = {
            ("SW1", 10), ("SW1", 20), ("SW1", 99),
            ("SW2", 110), ("SW2", 20), ("SW2", 99),
        }
        self.assertEqual(set(self.report.results), expected)

    def test_translation_works_both_directions_ok(self):
        """Different local vlan ids, one shared wire label, both directions."""
        ports = [
            make_port(
                "SW1", "p1", native_vlan=10, tagged=[110],
                egress={10: ("tag", 110)},
            ),
            make_port(
                "SW2", "p1", native_vlan=110, tagged=[110],
                egress={110: ("tag", 110)},
            ),
        ]
        topo = make_topology(
            ports,
            [make_link("SW1", "p1", "SW2", "p1")],
            pairs=[[("SW1", 10), ("SW2", 110)]],
        )
        report = analyze(topo)
        forward = report.results[("SW1", 10)]
        backward = report.results[("SW2", 110)]
        self.assertEqual(forward.boundary_events[0].status, "ok")
        self.assertEqual(backward.boundary_events[0].status, "ok")
        self.assertEqual(
            forward.boundary_events[0].received_domain, ("SW2", 110)
        )
        self.assertEqual(
            backward.boundary_events[0].received_domain, ("SW1", 10)
        )


if __name__ == "__main__":
    unittest.main()
