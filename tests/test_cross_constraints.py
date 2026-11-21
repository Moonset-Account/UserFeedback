"""Cross-constraint tests: nearest-ancestor wildcards, deletions, empty nodes.

These tests exercise the interlocks that only appear when several rules meet:

* ``*.example.`` and ``x.b.example.`` coexist, so ``b.example.`` is an empty
  node and ``y.b.example.`` has no nearer wildcard -> NXDOMAIN.
* Deleting ``x.b.example.`` removes ``b.example.``, so the *same* query
  ``y.b.example.`` may then use ``*.example.``.
* Adding and removing records must update empty nodes and answers together.
"""

from __future__ import annotations

import unittest

from authoritative_dns import (
    AnswerStatus,
    InvalidRecord,
    OutOfZoneName,
    Zone,
    ZoneError,
)

from .support import LookupAssertions


def build_interlock_zone() -> Zone:
    """Zone with a shallow wildcard and a deeper branch under b.example."""
    zone = Zone("example.", default_ttl=300)
    zone.add("example.", "A", "192.0.2.1")
    zone.add("*.example.", "A", "192.0.2.99")
    zone.add("*.example.", "TXT", "wild")
    zone.add("x.b.example.", "A", "192.0.2.20")
    return zone


class NearestAncestorInterlockTests(LookupAssertions, unittest.TestCase):
    def setUp(self):
        self.zone = build_interlock_zone()

    def test_shadowing_branch_suppresses_the_shallow_wildcard(self):
        # b.example exists as an empty node; *.example must not be consulted.
        self.assertIn("b.example.", self.zone.existing_names())
        self.assert_nodata(self.zone, "b.example.", "A")
        self.assert_nxdomain(self.zone, "y.b.example.", "A")
        self.assert_nxdomain(self.zone, "y.b.example.", "TXT")

    def test_exact_deep_name_still_answers(self):
        self.assert_answer(self.zone, "x.b.example.", "A", ["192.0.2.20"])

    def test_unrelated_names_keep_using_the_wildcard(self):
        self.assert_answer(self.zone, "other.example.", "A", ["192.0.2.99"],
                           wildcard=True)
        self.assert_answer(self.zone, "other.example.", "TXT", [("wild",)],
                           wildcard=True)

    def test_deleting_the_branch_enables_the_wildcard_same_query(self):
        self.assert_nxdomain(self.zone, "y.b.example.", "A")
        self.assertIn("b.example.", self.zone.existing_names())

        self.zone.remove("x.b.example.", "A")

        # Empty node must disappear from the existing-name index ...
        self.assertNotIn("x.b.example.", self.zone.existing_names())
        self.assertNotIn("b.example.", self.zone.existing_names())
        # ... and the very same query must now be wildcard-synthesised.
        self.assert_answer(self.zone, "y.b.example.", "A", ["192.0.2.99"],
                           wildcard=True)
        self.assert_answer(self.zone, "y.b.example.", "TXT", [("wild",)],
                           wildcard=True)

    def test_branch_deletion_does_not_leak_to_other_names(self):
        self.zone.remove("x.b.example.", "A")
        self.assert_answer(self.zone, "other.example.", "A", ["192.0.2.99"],
                           wildcard=True)
        # *.example still owns A only; TXT exists too, AAAA never did.
        self.assert_nodata(self.zone, "other.example.", "AAAA")

    def test_nearer_wildcard_wins_when_it_exists(self):
        self.zone.add("*.b.example.", "A", "192.0.2.77")
        self.assert_answer(self.zone, "y.b.example.", "A", ["192.0.2.77"],
                           wildcard=True)
        # A deeper wildcard does not accidentally answer a shallower name.
        self.assert_answer(self.zone, "other.example.", "A", ["192.0.2.99"],
                           wildcard=True)

    def test_nearer_wildcard_can_mask_the_shallow_one_with_nodata(self):
        self.zone.add("*.b.example.", "TXT", "near")
        # *.b.example exists but owns no A -> NODATA, not *.example's A.
        response = self.zone.lookup("y.b.example.", "A")
        self.assert_status(response, AnswerStatus.NODATA)
        self.assertFalse(response.wildcard)
        self.assert_answer(self.zone, "y.b.example.", "TXT", [("near",)],
                           wildcard=True)

    def test_removing_the_nearer_wildcard_restores_the_shallow_one(self):
        self.zone.add("*.b.example.", "A", "192.0.2.77")
        self.assert_answer(self.zone, "y.b.example.", "A", ["192.0.2.77"],
                           wildcard=True)
        self.zone.remove("*.b.example.", "A")
        self.assert_nxdomain(self.zone, "y.b.example.", "A")
        self.zone.remove("x.b.example.", "A")
        self.assert_answer(self.zone, "y.b.example.", "A", ["192.0.2.99"],
                           wildcard=True)


class EmptyNodeMaintenanceTests(LookupAssertions, unittest.TestCase):
    def test_adding_a_deeper_record_creates_intermediate_nodes(self):
        zone = Zone("example.")
        self.assertNotIn("b.example.", zone.existing_names())
        self.assert_nxdomain(zone, "b.example.", "A")

        zone.add("a.b.example.", "A", "192.0.2.20")

        self.assertIn("b.example.", zone.existing_names())
        self.assert_nodata(zone, "b.example.", "A")
        self.assert_nodata(zone, "b.example.", "TXT")
        self.assert_answer(zone, "a.b.example.", "A", ["192.0.2.20"])

    def test_intermediate_nodes_survive_while_siblings_remain(self):
        zone = Zone("example.")
        zone.add("a.b.example.", "A", "192.0.2.20")
        zone.add("c.b.example.", "A", "192.0.2.21")

        zone.remove("a.b.example.", "A")
        self.assertIn("b.example.", zone.existing_names())
        self.assert_nodata(zone, "b.example.", "A")

        zone.remove("c.b.example.", "A")
        self.assertNotIn("b.example.", zone.existing_names())
        self.assert_nxdomain(zone, "b.example.", "A")

    def test_partial_rrset_removal_keeps_the_name_and_other_types(self):
        zone = Zone("example.")
        zone.add("www.example.", "A", "192.0.2.10")
        zone.add("www.example.", "A", "192.0.2.11")
        zone.add("www.example.", "TXT", "hi")

        zone.remove("www.example.", "A", "192.0.2.10")
        self.assert_answer(zone, "www.example.", "A", ["192.0.2.11"])
        self.assert_answer(zone, "www.example.", "TXT", [("hi",)])

        zone.remove("www.example.", "A")
        self.assert_nodata(zone, "www.example.", "A")
        self.assert_answer(zone, "www.example.", "TXT", [("hi",)])

        zone.remove("www.example.", "TXT")
        self.assert_nxdomain(zone, "www.example.", "A")

    def test_adding_a_record_changes_nodata_into_answer(self):
        zone = Zone("example.")
        zone.add("only-a.example.", "A", "192.0.2.11")
        self.assert_nodata(zone, "only-a.example.", "AAAA")

        zone.add("only-a.example.", "AAAA", "2001:db8::11")
        self.assert_answer(zone, "only-a.example.", "AAAA", ["2001:db8::11"])

    def test_wildcard_node_is_created_and_removed_like_any_name(self):
        zone = Zone("example.")
        zone.add("x.b.example.", "A", "192.0.2.20")
        self.assert_nxdomain(zone, "y.b.example.", "A")

        zone.add("*.b.example.", "A", "192.0.2.77")
        self.assert_answer(zone, "y.b.example.", "A", ["192.0.2.77"],
                           wildcard=True)

        zone.remove("*.b.example.", "A")
        self.assert_nxdomain(zone, "y.b.example.", "A")
        self.assertIn("b.example.", zone.existing_names())


class DelegationInterlockTests(LookupAssertions, unittest.TestCase):
    def setUp(self):
        self.zone = build_interlock_zone()
        self.zone.add("child.example.", "NS", "ns1.child.example.")
        self.zone.add("child.example.", "NS", "ns2.child.example.")

    def test_delegation_wins_over_any_wildcard(self):
        for qtype in ("A", "TXT"):
            with self.subTest(qtype=qtype):
                response = self.zone.lookup("host.child.example.", qtype)
                self.assert_status(response, AnswerStatus.REFERRAL)
                self.assertFalse(response.wildcard)

    def test_delegation_ns_itself_is_a_referral(self):
        self.assert_referral(self.zone, "child.example.", "TXT",
                             ["ns1.child.example.", "ns2.child.example."])

    def test_cannot_add_data_below_a_delegation(self):
        with self.assertRaises(ZoneError):
            self.zone.add("host.child.example.", "A", "192.0.2.50")
        # Nothing changed.
        self.assert_referral(self.zone, "host.child.example.", "A",
                             ["ns1.child.example.", "ns2.child.example."])

    def test_cannot_add_non_ns_data_at_a_delegation_point(self):
        with self.assertRaises(ZoneError):
            self.zone.add("child.example.", "A", "192.0.2.50")
        self.assert_referral(self.zone, "child.example.", "A",
                             ["ns1.child.example.", "ns2.child.example."])

    def test_cannot_delegate_a_name_that_already_holds_data(self):
        with self.assertRaises(ZoneError):
            self.zone.add("x.b.example.", "NS", "ns1.x.b.example.")

    def test_cannot_delegate_above_existing_child_data(self):
        with self.assertRaises(ZoneError):
            self.zone.add("b.example.", "NS", "ns1.b.example.")
        # The branch is untouched and still suppresses the wildcard.
        self.assert_answer(self.zone, "x.b.example.", "A", ["192.0.2.20"])
        self.assert_nxdomain(self.zone, "y.b.example.", "A")

    def test_removing_the_delegation_restores_wildcard_behaviour(self):
        self.zone.remove("child.example.", "NS")
        self.assertNotIn("child.example.", self.zone.existing_names())
        self.assert_answer(self.zone, "host.child.example.", "A",
                           ["192.0.2.99"], wildcard=True)

    def test_removing_one_ns_record_keeps_the_delegation(self):
        self.zone.remove("child.example.", "NS", "ns1.child.example.")
        self.assert_referral(self.zone, "host.child.example.", "A",
                             ["ns2.child.example."])


class ZoneAdministrationTests(unittest.TestCase):
    def setUp(self):
        self.zone = Zone("example.")

    def test_duplicate_record_is_refused(self):
        self.zone.add("www.example.", "A", "192.0.2.10")
        with self.assertRaises(ZoneError):
            self.zone.add("WWW.example", "a", "192.0.2.10")

    def test_rrset_ttl_must_be_uniform(self):
        self.zone.add("www.example.", "A", "192.0.2.10", ttl=60)
        self.zone.add("www.example.", "A", "192.0.2.11", ttl=60)
        with self.assertRaises(ZoneError):
            self.zone.add("www.example.", "A", "192.0.2.12", ttl=120)
        self.assertEqual(
            [rr.value for rr in self.zone.lookup("www.example.", "A").records],
            ["192.0.2.10", "192.0.2.11"],
        )

    def test_removing_absent_data_is_refused_and_changes_nothing(self):
        self.zone.add("www.example.", "A", "192.0.2.10")
        with self.assertRaises(ZoneError):
            self.zone.remove("www.example.", "AAAA")
        with self.assertRaises(ZoneError):
            self.zone.remove("www.example.", "A", "192.0.2.99")
        self.assertEqual(
            [rr.value for rr in self.zone.lookup("www.example.", "A").records],
            ["192.0.2.10"],
        )

    def test_out_of_zone_record_is_refused(self):
        with self.assertRaises(OutOfZoneName):
            self.zone.add("www.example.net.", "A", "192.0.2.10")
        with self.assertRaises(InvalidRecord):
            self.zone.add("bad..name.", "A", "192.0.2.10")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
