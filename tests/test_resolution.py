"""End-to-end resolution tests over the synthetic sample zone.

Each test states the query, the expected DNS outcome and — for answers — the
owner name, TTL and value that must be returned.
"""

from __future__ import annotations

import unittest

from authoritative_dns import AnswerStatus, OutOfZoneName, ZoneError
from authoritative_dns.fixtures import DEFAULT_TTL, sample_zone

from .support import LookupAssertions


class ExactMatchTests(LookupAssertions, unittest.TestCase):
    def setUp(self):
        self.zone = sample_zone()

    def test_a_aaaa_and_txt_are_answered(self):
        self.assert_answer(self.zone, "www.example.", "A", ["192.0.2.10"])
        self.assert_answer(self.zone, "www.example.", "AAAA", ["2001:db8::10"])
        self.assert_answer(
            self.zone, "www.example.", "TXT", [("hello", "world")]
        )
        self.assert_answer(self.zone, "example.", "TXT", [("apex",)])
        self.assert_answer(self.zone, "example.", "A", ["192.0.2.1"])

    def test_lookup_is_case_and_dot_insensitive(self):
        for qname in ("WWW.EXAMPLE.", "www.example", "Www.Example"):
            with self.subTest(qname=qname):
                response = self.zone.lookup(qname, "a")
                self.assert_status(response, AnswerStatus.ANSWER)
                self.assertEqual(response.qname, "www.example.")
                self.assertEqual([rr.value for rr in response.records],
                                 ["192.0.2.10"])

    def test_answer_carries_ttl_and_owner(self):
        response = self.zone.lookup("www.example.", "A")
        record = response.records[0]
        self.assertEqual(record.ttl, DEFAULT_TTL)
        self.assertEqual(record.name, "www.example.")
        self.assertEqual(record.rtype, "A")

    def test_multi_record_ns_at_apex_is_not_a_referral(self):
        response = self.zone.lookup("example.", "NS")
        self.assert_status(response, AnswerStatus.ANSWER)
        self.assertEqual([rr.value for rr in response.records],
                         ["ns1.example."])


class NoDataTests(LookupAssertions, unittest.TestCase):
    """The name exists but has no RRset of the requested type."""

    def setUp(self):
        self.zone = sample_zone()

    def test_exact_name_without_requested_type(self):
        self.assert_nodata(self.zone, "only-a.example.", "AAAA")
        self.assert_nodata(self.zone, "only-a.example.", "TXT")
        self.assert_nodata(self.zone, "www.example.", "MX")

    def test_empty_non_terminal_exists_but_has_no_data(self):
        # b.example exists only because a.b and c.b exist below it.
        self.assertIn("b.example.", self.zone.existing_names())
        self.assert_nodata(self.zone, "b.example.", "A")
        self.assert_nodata(self.zone, "b.example.", "AAAA")
        self.assert_nodata(self.zone, "b.example.", "TXT")

    def test_empty_non_terminal_is_not_synthesised_from_wildcard(self):
        # *.example owns A and TXT, yet b.example must stay NODATA, not ANSWER.
        response = self.zone.lookup("b.example.", "A")
        self.assert_status(response, AnswerStatus.NODATA)
        self.assertFalse(response.wildcard)
        self.assertEqual(list(response.records), [])


class WildcardSynthesisTests(LookupAssertions, unittest.TestCase):
    def setUp(self):
        self.zone = sample_zone()

    def test_wildcard_answers_under_the_queried_name(self):
        response = self.zone.lookup("anything.example.", "A")
        self.assert_status(response, AnswerStatus.ANSWER)
        self.assertTrue(response.wildcard)
        self.assertEqual(response.source, "*.example.")
        self.assertEqual([rr.value for rr in response.records],
                         ["192.0.2.99"])
        self.assertEqual([rr.name for rr in response.records],
                         ["anything.example."])
        self.assertEqual([rr.ttl for rr in response.records], [DEFAULT_TTL])

    def test_wildcard_txt_synthesis(self):
        self.assert_answer(self.zone, "unknown.example.", "TXT",
                           [("wild",)], wildcard=True)

    def test_wildcard_exists_but_lacks_the_type_yields_nodata(self):
        # *.example owns A and TXT, not AAAA.
        self.assert_nodata(self.zone, "unknown.example.", "AAAA")
        self.assert_nodata(self.zone, "unknown.example.", "MX")

    def test_existing_ancestor_blocks_the_shallow_wildcard(self):
        # y.example has no data, and its closest encloser is the apex, so
        # *.example answers it.  y.b.example's closest encloser is the empty
        # node b.example, which has no *.b.example child, so it is NXDOMAIN.
        self.assert_answer(self.zone, "y.example.", "A", ["192.0.2.99"],
                           wildcard=True)
        self.assert_nxdomain(self.zone, "y.b.example.", "A")

    def test_nearest_ancestor_without_wildcard_child_is_nxdomain(self):
        # www.example exists, but it has no "*.www.example" child, so names
        # below it cannot be synthesised from *.example (one label only).
        self.assert_nxdomain(self.zone, "sub.www.example.", "A")
        self.assert_nxdomain(self.zone, "sub.www.example.", "TXT")


class DelegationTests(LookupAssertions, unittest.TestCase):
    def setUp(self):
        self.zone = sample_zone()

    def test_query_at_delegation_point_is_a_referral(self):
        self.assert_referral(
            self.zone,
            "child.example.",
            "A",
            ["ns1.child.example.", "ns2.child.example."],
        )

    def test_query_below_delegation_point_is_a_referral(self):
        for qtype in ("A", "AAAA", "TXT"):
            with self.subTest(qtype=qtype):
                self.assert_referral(
                    self.zone,
                    "host.child.example.",
                    qtype,
                    ["ns1.child.example.", "ns2.child.example."],
                )

    def test_closest_delegation_point_wins(self):
        self.assert_referral(
            self.zone, "x.deep.sub.example.", "A",
            ["ns1.deep.sub.example."],
        )

    def test_referral_does_not_synthesise_answers(self):
        # There is no data below the cut, and the referral must not be turned
        # into NXDOMAIN just because child-zone records are absent.
        response = self.zone.lookup("missing.child.example.", "TXT")
        self.assert_status(response, AnswerStatus.REFERRAL)
        self.assertTrue(all(rr.rtype == "NS" for rr in response.records))

    def test_delegation_beats_wildcard(self):
        # A wildcard must never be applied below a delegation point.
        response = self.zone.lookup("host.child.example.", "A")
        self.assert_status(response, AnswerStatus.REFERRAL)
        self.assertFalse(response.wildcard)


class ZoneScopeTests(unittest.TestCase):
    def test_out_of_zone_query_is_refused(self):
        zone = sample_zone()
        with self.assertRaises(OutOfZoneName):
            zone.lookup("example.net.", "A")
        with self.assertRaises(OutOfZoneName):
            zone.lookup("www.example.net.", "A")

    def test_invalid_query_type_is_refused(self):
        zone = sample_zone()
        with self.assertRaises(ZoneError):
            zone.lookup("www.example.", "A B")
        with self.assertRaises(TypeError):
            zone.lookup("www.example.", None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
