"""Unit tests for the fwmodel core: rect math, first-match semantics,
default deny, parsing/validation and region merging."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fwmodel
from fwmodel import Rect, ModelError


def rect(src="0.0.0.0/0", dst="0.0.0.0/0", proto="any", port="any"):
    return Rect(fwmodel.parse_cidr(src, "src"),
                fwmodel.parse_cidr(dst, "dst"),
                fwmodel.parse_proto(proto, "proto"),
                fwmodel.parse_port(port, "port"))


class RectMathTest(unittest.TestCase):
    def test_intersect_overlap(self):
        a = rect(src="10.0.0.0/8")
        b = rect(src="10.1.0.0/16")
        inter = fwmodel.intersect(a, b)
        self.assertIsNotNone(inter)
        self.assertEqual(inter.src, fwmodel.parse_cidr("10.1.0.0/16", "src"))

    def test_intersect_disjoint(self):
        a = rect(src="10.0.0.0/8")
        b = rect(src="11.0.0.0/8")
        self.assertIsNone(fwmodel.intersect(a, b))

    def test_intersect_proto_mismatch(self):
        a = rect(proto="tcp")
        b = rect(proto="udp")
        self.assertIsNone(fwmodel.intersect(a, b))

    def test_subtract_no_overlap(self):
        a = rect(src="10.0.0.0/8")
        b = rect(src="11.0.0.0/8")
        self.assertEqual(fwmodel.subtract(a, b), [a])

    def test_subtract_splits_source(self):
        a = rect(src="10.20.0.0/16")
        b = rect(src="10.20.7.0/24")
        pieces = fwmodel.subtract(a, b)
        self.assertEqual(len(pieces), 2)
        outer = fwmodel.parse_cidr("10.20.0.0/16", "x")
        inner = fwmodel.parse_cidr("10.20.7.0/24", "x")
        self.assertEqual(pieces[0].src, (outer[0], inner[0] - 1))
        self.assertEqual(pieces[1].src, (inner[1] + 1, outer[1]))

    def test_subtract_full_cover(self):
        a = rect(src="10.0.0.0/8")
        self.assertEqual(fwmodel.subtract(a, a), [])

    def test_subtract_proto_dimension(self):
        a = rect(proto="any")
        b = rect(proto="tcp")
        pieces = fwmodel.subtract(a, b)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].protos, frozenset({"udp"}))

    def test_subtract_port_dimension(self):
        a = rect(port="1-100")
        b = rect(port="50-60")
        pieces = fwmodel.subtract(a, b)
        self.assertEqual(sorted(p.port for p in pieces), [(1, 49), (61, 100)])

    def test_merge_adjacent_src(self):
        r1 = rect(src="10.20.0.0/24", port="80")
        r2 = rect(src="10.20.1.0/24", port="80")
        merged = fwmodel.merge_keyed([(r1, "k"), (r2, "k")])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][0].src, fwmodel.parse_cidr("10.20.0.0/23", "x"))

    def test_merge_key_mismatch_keeps_separate(self):
        r1 = rect(src="10.20.0.0/24", port="80")
        r2 = rect(src="10.20.1.0/24", port="80")
        merged = fwmodel.merge_keyed([(r1, "k1"), (r2, "k2")])
        self.assertEqual(len(merged), 2)


class FirstMatchTest(unittest.TestCase):
    def rules(self):
        return fwmodel.parse_rules([
            {"id": "broad", "action": "allow", "src": "10.0.0.0/8",
             "dst": "10.0.0.0/8", "proto": "any", "port": "any"},
            {"id": "fine", "action": "deny", "src": "10.20.0.0/16",
             "dst": "10.30.8.0/24", "proto": "tcp", "port": "5432"},
        ])

    def test_first_match_wins(self):
        # "fine" is shadowed by "broad": traffic is allowed.
        action, rid = fwmodel.decide(self.rules(),
                                     "10.20.1.5", "10.30.8.9", "tcp", 5432)
        self.assertEqual((action, rid), ("allow", "broad"))

    def test_default_deny(self):
        action, rid = fwmodel.decide(self.rules(),
                                     "192.0.2.1", "10.30.8.9", "tcp", 5432)
        self.assertEqual((action, rid), ("deny", None))

    def test_udp_not_matched_by_tcp_rule(self):
        rules = fwmodel.parse_rules([
            {"id": "tcp-only", "action": "allow", "src": "0.0.0.0/0",
             "dst": "0.0.0.0/0", "proto": "tcp", "port": "53"},
        ])
        action, rid = fwmodel.decide(rules, "1.2.3.4", "5.6.7.8", "udp", 53)
        self.assertEqual((action, rid), ("deny", None))

    def test_port_range_boundaries(self):
        rules = fwmodel.parse_rules([
            {"id": "web", "action": "allow", "src": "0.0.0.0/0",
             "dst": "0.0.0.0/0", "proto": "tcp", "port": "8000-9000"},
        ])
        self.assertEqual(fwmodel.decide(rules, "1.1.1.1", "2.2.2.2", "tcp", 8000)[0],
                         "allow")
        self.assertEqual(fwmodel.decide(rules, "1.1.1.1", "2.2.2.2", "tcp", 9000)[0],
                         "allow")
        self.assertEqual(fwmodel.decide(rules, "1.1.1.1", "2.2.2.2", "tcp", 9001)[0],
                         "deny")
        self.assertEqual(fwmodel.decide(rules, "1.1.1.1", "2.2.2.2", "tcp", 7999)[0],
                         "deny")


class ValidationTest(unittest.TestCase):
    def test_bad_cidr_rejected(self):
        with self.assertRaises(ModelError):
            fwmodel.parse_rules([{"id": "x", "action": "allow",
                                  "src": "999.0.0.0/8"}])

    def test_ipv6_rejected(self):
        with self.assertRaises(ModelError):
            fwmodel.parse_rules([{"id": "x", "action": "allow",
                                  "src": "::1/128"}])

    def test_bad_port_rejected(self):
        for bad in ("0", "65536", "9000-8000", "abc"):
            with self.assertRaises(ModelError, msg=f"port {bad}"):
                fwmodel.parse_rules([{"id": "x", "action": "allow",
                                      "port": bad}])

    def test_port_bounds_accepted(self):
        rules = fwmodel.parse_rules([
            {"id": "lo", "action": "allow", "port": "1"},
            {"id": "hi", "action": "allow", "port": "65535"},
        ])
        self.assertEqual(len(rules), 2)

    def test_bad_proto_rejected(self):
        with self.assertRaises(ModelError):
            fwmodel.parse_rules([{"id": "x", "action": "allow", "proto": "icmp"}])

    def test_bad_action_rejected(self):
        with self.assertRaises(ModelError):
            fwmodel.parse_rules([{"id": "x", "action": "permit"}])

    def test_missing_action_rejected(self):
        with self.assertRaises(ModelError):
            fwmodel.parse_rules([{"id": "x"}])

    def test_duplicate_id_rejected(self):
        with self.assertRaises(ModelError):
            fwmodel.parse_rules([{"id": "x", "action": "allow"},
                                 {"id": "x", "action": "deny"}])

    def test_business_requires_expect(self):
        with self.assertRaises(ModelError):
            fwmodel.parse_business([{"id": "b", "src": "10.0.0.0/8"}])


class DiffBasicTest(unittest.TestCase):
    def test_identical_sets_no_changes(self):
        raw = [{"id": "a", "action": "allow", "src": "10.0.0.0/8",
                "proto": "tcp", "port": "443"}]
        report = fwmodel.diff_rulesets(fwmodel.parse_rules(raw),
                                       fwmodel.parse_rules(raw))
        self.assertEqual(report["changes"], [])
        self.assertEqual(report["rule_only_changes"], [])

    def test_new_allow_rule_shows_deny_to_allow(self):
        current = fwmodel.parse_rules([])
        candidate = fwmodel.parse_rules([
            {"id": "new", "action": "allow", "src": "192.168.0.0/24",
             "dst": "10.0.0.0/24", "proto": "udp", "port": "53"},
        ])
        report = fwmodel.diff_rulesets(current, candidate)
        self.assertEqual(len(report["changes"]), 1)
        change = report["changes"][0]
        self.assertEqual(change["before"], {"action": "deny", "rule": None})
        self.assertEqual(change["after"], {"action": "allow", "rule": "new"})
        self.assertEqual(change["src"], "192.168.0.0/24")
        self.assertEqual(change["port"], "53")

    def test_removed_allow_rule_shows_allow_to_deny(self):
        current = fwmodel.parse_rules([
            {"id": "old", "action": "allow", "src": "192.168.0.0/24",
             "proto": "tcp", "port": "22"},
        ])
        candidate = fwmodel.parse_rules([])
        report = fwmodel.diff_rulesets(current, candidate)
        self.assertEqual(len(report["changes"]), 1)
        change = report["changes"][0]
        self.assertEqual(change["before"], {"action": "allow", "rule": "old"})
        self.assertEqual(change["after"], {"action": "deny", "rule": None})

    def test_partial_overlap_only_reports_intersection(self):
        current = fwmodel.parse_rules([
            {"id": "c", "action": "allow", "src": "10.0.0.0/9",
             "proto": "tcp", "port": "80"},
        ])
        candidate = fwmodel.parse_rules([
            {"id": "n", "action": "deny", "src": "10.0.0.0/8",
             "proto": "tcp", "port": "80"},
        ])
        report = fwmodel.diff_rulesets(current, candidate)
        # Only 10.0.0.0/9 (the overlap) flips allow->deny; 10.128.0.0/9
        # was and stays denied by default, so it must not appear.
        self.assertEqual(len(report["changes"]), 1)
        self.assertEqual(report["changes"][0]["src"], "10.0.0.0/9")
        self.assertEqual(report["changes"][0]["before"]["action"], "allow")
        self.assertEqual(report["changes"][0]["after"]["action"], "deny")


class BusinessCheckTest(unittest.TestCase):
    def test_partial_coverage_reports_only_unsatisfied_piece(self):
        rules = fwmodel.parse_rules([
            {"id": "allow-half", "action": "allow", "src": "10.0.0.0/9",
             "proto": "tcp", "port": "443"},
        ])
        business = fwmodel.parse_business([
            {"id": "b1", "src": "10.0.0.0/8", "proto": "tcp", "port": "443",
             "expect": "allow"},
        ])
        unsat = fwmodel.check_business(rules, business)
        self.assertEqual(len(unsat), 1)
        self.assertEqual(unsat[0]["src"], "10.128.0.0/9")
        self.assertEqual(unsat[0]["actual"], "deny")
        self.assertIsNone(unsat[0]["matched_rule"])

    def test_satisfied_entry_not_reported(self):
        rules = fwmodel.parse_rules([
            {"id": "a", "action": "allow", "src": "10.0.0.0/8",
             "proto": "tcp", "port": "443"},
        ])
        business = fwmodel.parse_business([
            {"id": "b1", "src": "10.1.0.0/16", "proto": "tcp", "port": "443",
             "expect": "allow"},
        ])
        self.assertEqual(fwmodel.check_business(rules, business), [])

    def test_wrong_action_rule_reported_with_rule_id(self):
        rules = fwmodel.parse_rules([
            {"id": "deny-web", "action": "deny", "src": "10.0.0.0/8",
             "proto": "tcp", "port": "80"},
        ])
        business = fwmodel.parse_business([
            {"id": "b1", "src": "10.0.0.0/8", "proto": "tcp", "port": "80",
             "expect": "allow"},
        ])
        unsat = fwmodel.check_business(rules, business)
        self.assertEqual(len(unsat), 1)
        self.assertEqual(unsat[0]["matched_rule"], "deny-web")
        self.assertEqual(unsat[0]["actual"], "deny")


if __name__ == "__main__":
    unittest.main()
