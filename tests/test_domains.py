"""Unit tests for domain statistics and spreading-rule gates."""

import unittest

from rolling_relayout import (
    Node,
    NodeSelector,
    Replica,
    SpreadRule,
    evaluate_domains,
    evaluate_start,
    find_balance_violations,
)


def _placement(mapping):
    return {rid: frozenset(nodes) for rid, nodes in mapping.items()}


class EvaluateDomainsTest(unittest.TestCase):
    def setUp(self):
        # rk-a hosts two service instances (r1, r2); rk-b hosts one (r3);
        # rk-c hosts none. r2 excludes rk-a via NotIn semantics.
        self.nodes = (
            Node("n1", {"rack": "rk-a", "zone": "z1"}, free_slots=1),
            Node("n2", {"rack": "rk-a", "zone": "z1"}, free_slots=1),
            Node("n3", {"rack": "rk-b", "zone": "z2"}, free_slots=2),
            Node("n4", {"rack": "rk-c", "zone": "z2"}, free_slots=2),
        )
        self.placement = _placement(
            {"r1": ["n1"], "r2": ["n2"], "r3": ["n3"]}
        )
        self.rack_rule = SpreadRule("rack-spread", "rack", max_skew=1)

    def test_eligible_domains_include_zero_replica_domains(self):
        replica = Replica("r1", NodeSelector())
        stat = evaluate_domains(self.nodes, self.placement, replica, self.rack_rule)

        self.assertEqual(stat.eligible_domains, ("rk-a", "rk-b", "rk-c"))
        self.assertEqual(
            stat.replica_counts, {"rk-a": 2, "rk-b": 1, "rk-c": 0}
        )
        self.assertEqual(stat.effective_min, 0)
        self.assertEqual(stat.max_count, 2)
        self.assertEqual(stat.max_skew_observed, 2)

    def test_excluded_rack_is_not_counted_as_zero_replica_rack(self):
        # r2 cannot run on rk-a (NotIn). Its current node n2 is an existing
        # (grandfathered) placement; rk-a must still be excluded from r2's
        # eligible set rather than reported as a zero-replica rack.
        replica = Replica("r2", NodeSelector(excluded={"rack": "rk-a"}))
        stat = evaluate_domains(self.nodes, self.placement, replica, self.rack_rule)

        self.assertEqual(stat.eligible_domains, ("rk-b", "rk-c"))
        self.assertNotIn("rk-a", stat.replica_counts)
        self.assertEqual(stat.replica_counts, {"rk-b": 1, "rk-c": 0})
        self.assertEqual(stat.eligible_domain_count, 2)

    def test_min_domains_threshold_forces_effective_min_to_zero(self):
        # Only rk-b and rk-c are eligible -> 2 eligible domains.
        replica = Replica("r2", NodeSelector(excluded={"rack": "rk-a"}))
        rule = SpreadRule("rack-spread", "rack", max_skew=1, min_domains=3)
        stat = evaluate_domains(self.nodes, self.placement, replica, rule)

        self.assertEqual(stat.eligible_domain_count, 2)
        self.assertEqual(stat.effective_min, 0)  # 2 < min_domains(3)
        self.assertEqual(stat.max_skew_observed, 1)

        # Without the threshold the real minimum (rk-c=0) is used directly;
        # counts must stay identical, only the effective minimum changes.
        relaxed = SpreadRule("rack-spread", "rack", max_skew=1, min_domains=1)
        stat_relaxed = evaluate_domains(self.nodes, self.placement, replica, relaxed)
        self.assertEqual(
            stat_relaxed.replica_counts, stat.replica_counts
        )
        self.assertEqual(stat_relaxed.effective_min, 0)

    def test_min_domains_threshold_with_all_domains_eligible(self):
        replica = Replica("r1", NodeSelector())
        rule = SpreadRule("rack-spread", "rack", max_skew=1, min_domains=3)
        stat = evaluate_domains(self.nodes, self.placement, replica, rule)
        # 3 eligible >= 3: the real global minimum (rk-c=0) applies.
        self.assertEqual(stat.effective_min, 0)

    def test_node_without_topology_label_is_ignored(self):
        nodes = (
            Node("n1", {"rack": "rk-a"}, free_slots=1),
            Node("n9", {}, free_slots=1),
        )
        replica = Replica("r1", NodeSelector())
        stat = evaluate_domains(nodes, _placement({"r1": ["n1"]}), replica, self.rack_rule)
        self.assertEqual(stat.eligible_domains, ("rk-a",))


class StartGateTest(unittest.TestCase):
    def setUp(self):
        self.nodes = (
            Node("n1", {"rack": "rk-a"}, free_slots=1),
            Node("n3", {"rack": "rk-b"}, free_slots=2),
        )
        self.replica = Replica("r1", NodeSelector())
        self.rule = SpreadRule("rack-spread", "rack", max_skew=1)

    def test_gate_uses_post_start_target_count_and_effective_min(self):
        placement = _placement({"r1": ["n1"]})  # rk-a=1, rk-b=0
        gate = evaluate_start(
            self.nodes, placement, self.replica, self.nodes[1], self.rule
        )
        # After start: rk-b -> 1, global min 0, skew 1 <= max_skew 1.
        self.assertTrue(gate.allowed)
        self.assertEqual(gate.target_domain, "rk-b")
        self.assertEqual(gate.observed_skew, 1)
        self.assertEqual(gate.effective_min, 0)
        self.assertEqual(gate.replica_counts_after, {"rk-a": 1, "rk-b": 1})

    def test_gate_rejects_skew_above_max_skew(self):
        # rk-a already holds 2 instances (r1 double-running under surge).
        placement = _placement({"r1": ["n1", "n1-other"]})
        nodes = (
            Node("n1", {"rack": "rk-a"}, free_slots=2),
            Node("n1-other", {"rack": "rk-a"}, free_slots=1),
            Node("n3", {"rack": "rk-b"}, free_slots=2),
        )
        rule = SpreadRule("rack-spread", "rack", max_skew=1)
        # Starting a third instance on rk-a: 3 - min(0) = 3 > 1.
        gate = evaluate_start(nodes, placement, self.replica, nodes[0], rule)
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.observed_skew, 3)
        self.assertEqual(gate.replica_counts_after, {"rk-a": 3, "rk-b": 0})

    def test_gate_rejects_node_failing_selector(self):
        picky = Replica("r9", NodeSelector(required={"tier": "gold"}))
        gate = evaluate_start(
            self.nodes, _placement({}), picky, self.nodes[0], self.rule
        )
        self.assertFalse(gate.allowed)
        self.assertIn("selector", gate.reason)

    def test_gate_threshold_zero_min_makes_second_domain_allowed(self):
        # One eligible domain only; min_domains=2 -> effective min 0.
        nodes = (
            Node("n1", {"rack": "rk-a", "tier": "gold"}, free_slots=2),
            Node("n2", {"rack": "rk-b", "tier": "silver"}, free_slots=2),
        )
        replica = Replica("r1", NodeSelector(required={"tier": "gold"}))
        rule = SpreadRule("rack-spread", "rack", max_skew=1, min_domains=2)
        placement = _placement({"r1": ["n1"]})
        gate = evaluate_start(nodes, placement, replica, nodes[0], rule)
        # Only rk-a eligible; post-start rk-a=2, effective min 0 -> skew 2 > 1.
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.eligible_domain_count, 1)
        self.assertEqual(gate.effective_min, 0)
        self.assertEqual(gate.observed_skew, 2)


class BalanceViolationsTest(unittest.TestCase):
    def test_existing_layout_may_violate_new_rules(self):
        nodes = (
            Node("n1", {"rack": "rk-a"}, free_slots=1),
            Node("n2", {"rack": "rk-a"}, free_slots=1),
            Node("n3", {"rack": "rk-b"}, free_slots=2),
        )
        replicas = (
            Replica("r1", NodeSelector(), current_node="n1"),
            Replica("r2", NodeSelector(), current_node="n2"),
        )
        rule = SpreadRule("rack-spread", "rack", max_skew=1)
        placement = {"r1": frozenset(["n1"]), "r2": frozenset(["n2"])}
        violations = find_balance_violations(nodes, placement, replicas, [rule])
        self.assertEqual(len(violations), 2)
        for replica, violated_rule, stat in violations:
            self.assertEqual(violated_rule.name, "rack-spread")
            self.assertEqual(stat.max_skew_observed, 2)


if __name__ == "__main__":
    unittest.main()
