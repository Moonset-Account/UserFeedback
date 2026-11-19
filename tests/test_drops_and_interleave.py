"""Rejected-tag drop records and interleaved boundary classifications."""

import unittest

from vlancheck.analyzer import analyze

from tests.helpers import make_link, make_port, make_topology


def build_drop_topology():
    """SW1 sends tag 30; SW2 accepts only tags 10/20, so 30 must be dropped."""
    ports = [
        make_port("SW1", "p1", native_vlan=1, tagged=[10, 20],
                  egress={1: ("untag",), 30: ("tag", 30), 10: ("tag", 10)}),
        make_port("SW1", "edge30", native_vlan=30, tagged=[],
                  egress={30: ("untag",)}),
        make_port("SW1", "edge10", native_vlan=10, tagged=[],
                  egress={10: ("untag",)}),
        make_port("SW2", "p1", native_vlan=1, tagged=[10, 20],
                  egress={1: ("untag",), 10: ("tag", 10), 20: ("tag", 20)}),
        make_port("SW2", "edge10", native_vlan=10, tagged=[],
                  egress={10: ("untag",)}),
    ]
    links = [make_link("SW1", "p1", "SW2", "p1")]
    pairs = [[("SW1", 10), ("SW2", 10)]]
    return make_topology(ports, links, pairs)


class DropRecordTests(unittest.TestCase):
    def setUp(self):
        self.report = analyze(build_drop_topology())

    def test_rejected_tag_is_recorded_as_drop(self):
        root_drops = [d for d in self.report.drops if d.root == ("SW1", 30)]
        self.assertEqual(len(root_drops), 1)
        drop = root_drops[0]
        self.assertEqual(drop.rejected_tag, 30)
        self.assertEqual(drop.egress_port, ("SW1", "p1"))
        self.assertEqual(drop.ingress_port, ("SW2", "p1"))

    def test_dropped_frame_never_becomes_a_domain_or_event(self):
        result = self.report.results[("SW1", 30)]
        self.assertEqual(result.reached_domains, {("SW1", 30)})
        self.assertEqual(result.boundary_events, [])
        self.assertIsNone(result.first_leak)

    def test_accepted_tag_on_same_link_still_works(self):
        # The same cable carries tag 10 acceptably, proving the drop is
        # label-specific rather than link-wide.
        result = self.report.results[("SW1", 10)]
        self.assertIn(("SW2", 10), result.reached_domains)
        ok = [e for e in result.boundary_events if e.status == "ok"]
        self.assertTrue(ok)
        self.assertFalse(
            any(d.root == ("SW1", 10) for d in self.report.drops)
        )

    def test_drop_serializes(self):
        payload = self.report.to_dict()
        self.assertTrue(any(d["rejected_tag"] == 30 for d in payload["drops"]))


class InterleavedBoundaryTests(unittest.TestCase):
    def test_unmapped_crossing_is_neither_ok_nor_leak(self):
        """No prescribed pair: crossing is reported 'unmapped', not a leak."""
        ports = [
            make_port("SW1", "p1", native_vlan=7, tagged=[7],
                      egress={7: ("tag", 7)}),
            make_port("SW2", "p1", native_vlan=8, tagged=[7],
                      egress={8: ("untag",), 7: ("tag", 7)}),
        ]
        topo = make_topology(
            ports, [make_link("SW1", "p1", "SW2", "p1")], pairs=[]
        )
        report = analyze(topo)
        result = report.results[("SW1", 7)]
        event = result.boundary_events[0]
        self.assertEqual(event.status, "unmapped")
        self.assertFalse(event.is_leak)
        self.assertIsNone(result.first_leak)
        self.assertFalse(report.has_leak)

    def test_mapped_ok_and_leak_interleave_on_paired_root(self):
        """A paired root floods two peers: one ok, one foreign (leak)."""
        ports = [
            make_port("SW1", "p-ok", native_vlan=1, tagged=[40],
                      egress={1: ("untag",), 40: ("tag", 40)}),
            make_port("SW1", "p-bad", native_vlan=1, tagged=[40],
                      egress={1: ("untag",), 40: ("tag", 40)}),
            make_port("SW1", "edge", native_vlan=40, tagged=[],
                      egress={40: ("untag",)}),
            # Paired peer (ok).
            make_port("SW2", "p1", native_vlan=1, tagged=[40],
                      egress={1: ("untag",), 40: ("tag", 40)}),
            # Foreign domain although a pair for SW1:40 exists (leak):
            # SW3 maps wire tag 40 to its local vlan99 via tag translation.
            make_port("SW3", "p1", native_vlan=1, tagged=[40],
                      egress={1: ("untag",), 99: ("tag", 40)}),
        ]
        topo = make_topology(
            ports,
            [
                make_link("SW1", "p-ok", "SW2", "p1"),
                make_link("SW1", "p-bad", "SW3", "p1"),
            ],
            pairs=[[("SW1", 40), ("SW2", 40)]],
        )
        report = analyze(topo)
        result = report.results[("SW1", 40)]
        statuses = {
            e.received_domain: e.status for e in result.boundary_events
        }
        self.assertEqual(statuses[("SW2", 40)], "ok")
        # SW3 translates wire tag 40 into its local vlan99, which is not
        # the prescribed peer domain SW2:40: the leak receiver is SW3:99.
        self.assertEqual(statuses[("SW3", 99)], "leak")
        # Deterministic first leak points at the leaker.
        self.assertEqual(
            result.first_leak.ingress_port, ("SW3", "p1")
        )
        self.assertEqual(
            result.first_leak.received_domain, ("SW3", 99)
        )
        self.assertEqual(
            result.first_leak.expected_domain, ("SW2", 40)
        )

    def test_unmapped_root_never_leaks_even_when_domains_differ(self):
        """An unpaired root domain is 'unmapped', never a leak."""
        ports = [
            make_port("SW1", "p-free", native_vlan=1, tagged=[40],
                      egress={1: ("untag",), 50: ("tag", 40)}),
            make_port("SW1", "edge", native_vlan=50, tagged=[],
                      egress={50: ("untag",)}),
            make_port("SW4", "p1", native_vlan=1, tagged=[40],
                      egress={1: ("untag",), 77: ("tag", 40)}),
        ]
        topo = make_topology(
            ports,
            [make_link("SW1", "p-free", "SW4", "p1")],
            # SW1:50 is deliberately absent from every pair.
            pairs=[],
        )
        report = analyze(topo)
        result = report.results[("SW1", 50)]
        event = result.boundary_events[0]
        self.assertEqual(event.received_domain, ("SW4", 77))
        self.assertEqual(event.status, "unmapped")
        self.assertFalse(event.is_leak)
        self.assertIsNone(result.first_leak)
        self.assertFalse(report.has_leak)

    def test_boundary_value_of_pairing_only_checks_paired_domains(self):
        """A mismatched crossing without a pair must not be raised as leak."""
        ports = [
            make_port("SW1", "p1", native_vlan=40, tagged=[40],
                      egress={40: ("tag", 40)}),
            make_port("SW2", "p1", native_vlan=41, tagged=[40],
                      egress={41: ("untag",), 40: ("tag", 40)}),
        ]
        topo = make_topology(
            ports, [make_link("SW1", "p1", "SW2", "p1")], pairs=[]
        )
        report = analyze(topo)
        self.assertFalse(report.has_leak)
        self.assertEqual(
            report.results[("SW1", 40)].boundary_events[0].status,
            "unmapped",
        )


if __name__ == "__main__":
    unittest.main()
