"""Tests pinned to the bundled rule-reordering sample (samples/).

Scenario: in the current rule set the broad rule "allow-all-internal"
(10.0.0.0/8 -> 10.0.0.0/8, any/any, allow) shadows the fine-grained rules
"deny-office-to-db" and "allow-ops-to-db".  The candidate set moves the
two fine rules ahead of the broad one.  The diff must show exactly which
sub-ranges flip, and the business check must list the ranges the
candidate still does not satisfy.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fwmodel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")


def load(name, parser):
    with open(os.path.join(SAMPLES, name), "r", encoding="utf-8") as fh:
        return parser(json.load(fh))


class ReorderSampleDiffTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.current = load("reorder_current.json", fwmodel.parse_rules)
        cls.candidate = load("reorder_candidate.json", fwmodel.parse_rules)
        cls.report = fwmodel.diff_rulesets(cls.current, cls.candidate)

    def test_exactly_two_changed_subranges(self):
        # 10.20.0.0/16 minus the ops /24 splits into two source pieces,
        # both flipping allow -> deny on tcp/5432 towards 10.30.8.0/24.
        changes = self.report["changes"]
        self.assertEqual(len(changes), 2)
        flat = set()
        for c in changes:
            piece = c["src"]
            flat.update(piece if isinstance(piece, list) else [piece])
        # exact CIDR decomposition of 10.20.0.0/16 - 10.20.7.0/24
        self.assertEqual(flat, {"10.20.0.0/22", "10.20.4.0/23", "10.20.6.0/24",
                                "10.20.8.0/21", "10.20.16.0/20",
                                "10.20.32.0/19", "10.20.64.0/18",
                                "10.20.128.0/17"})
        for c in changes:
            self.assertEqual(c["dst"], "10.30.8.0/24")
            self.assertEqual(c["proto"], "tcp")
            self.assertEqual(c["port"], "5432")
            self.assertEqual(c["before"],
                             {"action": "allow", "rule": "allow-all-internal"})
            self.assertEqual(c["after"],
                             {"action": "deny", "rule": "deny-office-to-db"})

    def test_ops_subnet_is_rule_only_change(self):
        # The ops /24 stays allowed but is now matched by the fine rule
        # instead of the broad one: no action flip, only a rule change.
        rule_only = self.report["rule_only_changes"]
        self.assertEqual(len(rule_only), 1)
        entry = rule_only[0]
        self.assertEqual(entry["src"], "10.20.7.0/24")
        self.assertEqual(entry["dst"], "10.30.8.0/24")
        self.assertEqual(entry["proto"], "tcp")
        self.assertEqual(entry["port"], "5432")
        self.assertEqual(entry["before"],
                         {"action": "allow", "rule": "allow-all-internal"})
        self.assertEqual(entry["after"],
                         {"action": "allow", "rule": "allow-ops-to-db"})

    def test_unaffected_traffic_not_reported(self):
        # Everything else inside 10/8 -> 10/8 keeps hitting
        # allow-all-internal; nothing outside the db flow may appear.
        for c in self.report["changes"] + self.report["rule_only_changes"]:
            self.assertEqual(c["dst"], "10.30.8.0/24")
            self.assertEqual(c["port"], "5432")

    def test_point_evaluation_matches_report(self):
        # Spot-check the semantics behind the report with decide().
        probe_cases = [
            # (src, expected current action, expected candidate action)
            ("10.20.3.9", "allow", "deny"),    # office, now denied
            ("10.20.7.9", "allow", "allow"),   # ops exception, still allowed
            ("10.20.200.9", "allow", "deny"),  # upper half of office /16
            ("10.9.9.9", "allow", "allow"),    # unrelated internal traffic
            ("172.16.9.9", "deny", "deny"),    # external: default deny both
        ]
        for src, before, after in probe_cases:
            got_before = fwmodel.decide(self.current, src, "10.30.8.10",
                                        "tcp", 5432)[0]
            got_after = fwmodel.decide(self.candidate, src, "10.30.8.10",
                                       "tcp", 5432)[0]
            self.assertEqual((got_before, got_after), (before, after),
                             msg=f"src={src}")


class ReorderSampleBusinessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.candidate = load("reorder_candidate.json", fwmodel.parse_rules)
        cls.business = load("reorder_business.json", fwmodel.parse_business)
        cls.unsat = fwmodel.check_business(cls.candidate, cls.business)

    def test_office_requirement_partially_unsatisfied(self):
        # biz-office-to-db expects the whole 10.20.0.0/16 to be denied,
        # but the candidate allows the ops /24 exception: only that
        # sub-range must be listed.
        entries = [u for u in self.unsat if u["business"] == "biz-office-to-db"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["src"], "10.20.7.0/24")
        self.assertEqual(entries[0]["expected"], "deny")
        self.assertEqual(entries[0]["actual"], "allow")
        self.assertEqual(entries[0]["matched_rule"], "allow-ops-to-db")

    def test_ops_requirement_fully_satisfied(self):
        entries = [u for u in self.unsat if u["business"] == "biz-ops-to-db"]
        self.assertEqual(entries, [])

    def test_partner_requirement_unsatisfied_by_default_deny(self):
        entries = [u for u in self.unsat if u["business"] == "biz-partner-to-db"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["src"], "172.16.9.0/24")
        self.assertEqual(entries[0]["expected"], "allow")
        self.assertEqual(entries[0]["actual"], "deny")
        self.assertIsNone(entries[0]["matched_rule"])

    def test_total_unsatisfied_count(self):
        self.assertEqual(len(self.unsat), 2)


if __name__ == "__main__":
    unittest.main()
