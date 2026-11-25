"""Additional boundary tests: per-replica selectors, min_domains gates,
stopped replicas, deterministic output and multi-replica co-location."""

import unittest

from rolling_relayout import (
    Action,
    Node,
    NodeSelector,
    Replica,
    SpreadRule,
    evaluate_start,
    find_plan,
)


class PerReplicaSelectorTest(unittest.TestCase):
    def test_move_uses_only_nodes_matching_that_replica_selector(self):
        # r1 is grandfathered on a silver node; the gold racks are rk-a/rk-c,
        # and rk-a already holds two service instances -> the cluster starts
        # with skew 2 and BFS must produce a two-action start/stop rebalance.
        nodes = (
            Node("g1", {"rack": "rk-a", "tier": "gold"}, free_slots=0),
            Node("g2", {"rack": "rk-a", "tier": "gold"}, free_slots=0),
            Node("s1", {"rack": "rk-b", "tier": "silver"}, free_slots=0),
            Node("g3", {"rack": "rk-c", "tier": "gold"}, free_slots=2),
        )
        replicas = (
            Replica("r1", NodeSelector(required={"tier": "gold"}), current_node="s1"),
            Replica("r2", NodeSelector(), current_node="g1"),
            Replica("r3", NodeSelector(), current_node="g2"),
        )
        rule = SpreadRule("rack-spread", "rack", max_skew=1)
        plan = find_plan(nodes, replicas, [rule], min_ready=3, max_surge=1)
        self.assertTrue(plan.feasible)
        # r1 must leave the non-conforming silver node and one more replica
        # must leave the over-full rk-a: two start+stop moves = 4 actions.
        self.assertEqual(plan.length, 4)

        # r1 can never be started on the silver node regardless of the plan
        # the search happens to pick.
        initial_placement = {
            r.replica_id: frozenset([r.current_node]) if r.current_node else frozenset()
            for r in replicas
        }
        r1 = replicas[0]
        silver_gate = evaluate_start(nodes, initial_placement, r1, nodes[2], rule)
        self.assertFalse(silver_gate.allowed)
        self.assertIn("selector", silver_gate.reason)
        gold_gate = evaluate_start(nodes, initial_placement, r1, nodes[3], rule)
        self.assertTrue(gold_gate.allowed)
        # r1's eligible domains never include the silver rack.
        self.assertEqual(gold_gate.eligible_domain_count, 2)
        self.assertNotIn("rk-b", gold_gate.replica_counts_after)

        # And whatever the plan does, r1's final node must satisfy its selector.
        r1_final = next(n for n in nodes if n.node_id == plan.final_placement[0])
        self.assertTrue(r1.selector.matches(r1_final))
        for step in plan.steps:
            if step.action.kind == "start" and step.action.replica_id == "r1":
                target = next(n for n in nodes if n.node_id == step.action.node_id)
                self.assertEqual(target.labels["tier"], "gold")


class MinDomainsPlannerTest(unittest.TestCase):
    def test_threshold_makes_single_eligible_domain_unusable_for_second_start(self):
        # Gold nodes exist only in rk-a. min_domains=2 forces effective min 0,
        # so a second gold replica would see skew 2 > max_skew 1: infeasible.
        nodes = (
            Node("g1", {"rack": "rk-a", "tier": "gold"}, free_slots=3),
            Node("s1", {"rack": "rk-b", "tier": "silver"}, free_slots=3),
        )
        replicas = (
            Replica("r1", NodeSelector(required={"tier": "gold"}), current_node="g1"),
            Replica("r2", NodeSelector(required={"tier": "gold"}), current_node=None),
        )
        rule = SpreadRule("rack-spread", "rack", max_skew=1, min_domains=2)
        plan = find_plan(nodes, replicas, [rule], min_ready=1, max_surge=2)
        self.assertFalse(plan.feasible)
        # The stopped replica stays stopped in the reported final placement.
        self.assertEqual(plan.final_placement, ("g1", None))

    def test_relaxing_threshold_makes_the_same_layout_feasible(self):
        nodes = (
            Node("g1", {"rack": "rk-a", "tier": "gold"}, free_slots=3),
            Node("s1", {"rack": "rk-b", "tier": "silver"}, free_slots=3),
        )
        replicas = (
            Replica("r1", NodeSelector(required={"tier": "gold"}), current_node="g1"),
            Replica("r2", NodeSelector(required={"tier": "gold"}), current_node=None),
        )
        rule = SpreadRule("rack-spread", "rack", max_skew=1, min_domains=1)
        plan = find_plan(nodes, replicas, [rule], min_ready=1, max_surge=2)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.length, 1)
        self.assertEqual(plan.actions[0].kind, "start")
        gate = plan.steps[0].gate_results["rack-spread"]
        self.assertTrue(gate.allowed)
        self.assertEqual(gate.eligible_domain_count, 1)
        self.assertEqual(gate.effective_min, 1)  # sole eligible domain min=1


class StoppedReplicaTest(unittest.TestCase):
    def test_initially_stopped_replica_can_be_started_under_min_ready(self):
        nodes = (
            Node("n1", {"rack": "rk-a"}, free_slots=2),
            Node("n2", {"rack": "rk-b"}, free_slots=2),
        )
        replicas = (
            Replica("r1", NodeSelector(), current_node="n1"),
            Replica("r2", NodeSelector(), current_node=None),
        )
        rule = SpreadRule("rack-spread", "rack", max_skew=1)
        plan = find_plan(nodes, replicas, [rule], min_ready=1, max_surge=1)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.length, 1)
        self.assertEqual(plan.actions[0], Action("start", "r2", "n2"))
        self.assertEqual(plan.final_placement, ("n1", "n2"))


class DeterminismTest(unittest.TestCase):
    def test_repeated_runs_return_identical_plans(self):
        nodes = (
            Node("n1", {"rack": "rk-a"}, free_slots=0),
            Node("n2", {"rack": "rk-a"}, free_slots=0),
            Node("n3", {"rack": "rk-b"}, free_slots=0),
            Node("n4", {"rack": "rk-b"}, free_slots=0),
            Node("n5", {"rack": "rk-c"}, free_slots=2),
            Node("n6", {"rack": "rk-d"}, free_slots=2),
        )
        replicas = tuple(
            Replica(f"r{i}", NodeSelector(), current_node=node_id)
            for i, node_id in enumerate(("n1", "n2", "n3", "n4"))
        )
        rule = SpreadRule("rack-spread", "rack", max_skew=1)
        first = find_plan(nodes, replicas, [rule], min_ready=4, max_surge=1)
        for _ in range(3):
            again = find_plan(nodes, replicas, [rule], min_ready=4, max_surge=1)
            self.assertEqual(first.actions, again.actions)
            self.assertEqual(first.final_placement, again.final_placement)


class CoLocationTest(unittest.TestCase):
    def test_two_replicas_may_share_a_node_within_slot_budget(self):
        # One node with two free slots: surge double-run of a replica on the
        # same node is permitted by slot accounting and then drained.
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
        plan = find_plan(nodes, replicas, [rule], min_ready=2, max_surge=1)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.length, 2)
        # Start on n3 (free slots=2), then stop an rk-a instance.
        self.assertEqual(plan.steps[0].action.node_id, "n3")
        self.assertEqual(plan.steps[1].action.kind, "stop")
        final_racks = {
            node_id: next(n for n in nodes if n.node_id == node_id).labels["rack"]
            for node_id in plan.final_placement
        }
        self.assertEqual(sorted(final_racks.values()), ["rk-a", "rk-b"])


if __name__ == "__main__":
    unittest.main()
