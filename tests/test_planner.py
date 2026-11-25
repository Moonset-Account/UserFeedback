"""End-to-end planner tests with an independent rollout re-simulation."""

import unittest

from rolling_relayout import (
    Action,
    Node,
    NodeSelector,
    Planner,
    PlannerInput,
    Replica,
    SpreadRule,
    evaluate_domains,
    evaluate_start,
    find_plan,
)


class _SlotValidator:
    """Tracks free slots relative to the initial layout."""

    def __init__(self, planner_input):
        self.free = {n.node_id: n.free_slots for n in planner_input.nodes}

    def check_start(self, node_id):
        assert self.free[node_id] > 0, f"node {node_id} has no free slot"
        self.free[node_id] -= 1

    def record_stop(self, node_id):
        self.free[node_id] += 1


def replay(planner_input, plan):
    """Re-simulate every action from scratch without trusting the planner.

    Checks free-slot accounting, selectors, the desired+surge cap, min_ready
    on stops, both spreading gates (with independently recomputed values) on
    every start, and final balanced-layout conditions.
    """

    nodes = planner_input.nodes
    node_by_id = {n.node_id: n for n in nodes}
    replica_by_id = {r.replica_id: r for r in planner_input.replicas}
    cap = len(planner_input.replicas) + planner_input.max_surge
    slots = _SlotValidator(planner_input)
    running = {
        r.replica_id: ({r.current_node} if r.current_node else set())
        for r in planner_input.replicas
    }

    assert len(plan.steps) == len(plan.actions)
    for step, action in zip(plan.steps, plan.actions):
        assert step.action == action
        running_before = sum(len(v) for v in running.values())
        assert step.running_before == running_before
        replica = replica_by_id[action.replica_id]
        node = node_by_id[action.node_id]
        placement = {rid: frozenset(v) for rid, v in running.items()}

        if action.kind == "start":
            assert action.node_id not in running[action.replica_id]
            slots.check_start(action.node_id)
            assert replica.selector.matches(node)
            assert running_before + 1 <= cap
            for rule in planner_input.rules:
                gate = evaluate_start(nodes, placement, replica, node, rule)
                assert gate.allowed, f"{rule.name}: {gate.reason}"
                recorded = step.gate_results[rule.name]
                assert recorded.allowed
                assert recorded.observed_skew == gate.observed_skew
                assert recorded.replica_counts_after == gate.replica_counts_after
                stat_before = step.domain_stats_before[rule.name]
                expected_stat = evaluate_domains(nodes, placement, replica, rule)
                assert stat_before.eligible_domains == expected_stat.eligible_domains
                assert stat_before.replica_counts == expected_stat.replica_counts
                assert stat_before.effective_min == expected_stat.effective_min
            running[action.replica_id].add(action.node_id)
        else:
            assert step.gate_results == {}
            assert action.node_id in running[action.replica_id]
            assert running_before - 1 >= planner_input.min_ready
            slots.record_stop(action.node_id)
            running[action.replica_id].discard(action.node_id)

        assert step.running_after == sum(len(v) for v in running.values())

    assert all(len(v) == 1 for v in running.values())
    placement = {rid: frozenset(v) for rid, v in running.items()}
    assert tuple(plan.final_placement) == tuple(next(iter(v)) for v in running.values())
    for replica in planner_input.replicas:
        for rule in planner_input.rules:
            stat = evaluate_domains(nodes, placement, replica, rule)
            assert stat.max_skew_observed <= rule.max_skew
    return placement


def _four_rack_cluster():
    # Initial rack distribution (2, 2, 0, 0): skew 2 with max_skew=1, so two
    # replicas must leave rk-a/rk-b. Each move is start+stop -> 4 actions.
    nodes = (
        Node("n1", {"rack": "rk-a"}, free_slots=0),
        Node("n2", {"rack": "rk-a"}, free_slots=0),
        Node("n3", {"rack": "rk-b"}, free_slots=0),
        Node("n4", {"rack": "rk-b"}, free_slots=0),
        Node("n5", {"rack": "rk-c"}, free_slots=2),
        Node("n6", {"rack": "rk-d"}, free_slots=2),
    )
    replicas = (
        Replica("r1", NodeSelector(), current_node="n1"),
        Replica("r2", NodeSelector(), current_node="n2"),
        Replica("r3", NodeSelector(), current_node="n3"),
        Replica("r4", NodeSelector(), current_node="n4"),
    )
    rule = SpreadRule("rack-spread", "rack", max_skew=1)
    return nodes, replicas, rule


def _two_rack_stop_first_cluster():
    # Two racks holding 3 vs 1 (skew 2). No free slot on the full rack and no
    # surge budget: rebalancing is possible only by stopping first.
    nodes = (
        Node("n1", {"rack": "rk-a"}, free_slots=0),
        Node("n2", {"rack": "rk-a"}, free_slots=1),
        Node("n3", {"rack": "rk-a"}, free_slots=0),
        Node("n4", {"rack": "rk-b"}, free_slots=2),
    )
    replicas = (
        Replica("r1", NodeSelector(), current_node="n1"),
        Replica("r2", NodeSelector(), current_node="n2"),
        Replica("r3", NodeSelector(), current_node="n3"),
        Replica("r4", NodeSelector(), current_node="n4"),
    )
    rule = SpreadRule("rack-spread", "rack", max_skew=1)
    return nodes, replicas, rule


class RebalanceTest(unittest.TestCase):
    def test_minimum_action_rack_rebalance_with_surge(self):
        nodes, replicas, rule = _four_rack_cluster()
        plan = find_plan(nodes, replicas, [rule], min_ready=4, max_surge=1)

        self.assertTrue(plan.feasible)
        # Initial (2,2,0,0), target (1,1,1,1): two replicas must move and
        # each move costs start + stop, so 4 actions minimum.
        self.assertEqual(plan.length, 4)
        kinds = [a.kind for a in plan.actions]
        # min_ready=4 forbids any stop while 4 run: first action must start.
        self.assertEqual(kinds[0], "start")
        self.assertEqual(sum(1 for k in kinds if k == "start"), 2)
        self.assertEqual(sum(1 for k in kinds if k == "stop"), 2)

        placement = replay(PlannerInput(nodes, replicas, (rule,), 4, 1), plan)
        counts: dict[str, int] = {}
        for nodes_used in placement.values():
            node_id = next(iter(nodes_used))
            rack = next(n for n in nodes if n.node_id == node_id).labels["rack"]
            counts[rack] = counts.get(rack, 0) + 1
        self.assertEqual(counts, {"rk-a": 1, "rk-b": 1, "rk-c": 1, "rk-d": 1})

    def test_plan_is_minimum_via_depth_boundary(self):
        nodes, replicas, rule = _four_rack_cluster()
        planner = Planner(PlannerInput(nodes, replicas, (rule,), 4, 1))
        plan = planner.plan()
        # Initial skew is 2, so depth 0 cannot be a goal; two replicas have
        # to change nodes, which needs at least two start + two stop actions.
        self.assertEqual(plan.length, 4)
        for step in plan.steps:
            self.assertIn("rack-spread", step.domain_stats_before)
        # First step evidence: the start target has an eligible domain entry.
        first = plan.steps[0]
        stat = first.domain_stats_before["rack-spread"]
        target_rack = next(
            n for n in nodes if n.node_id == first.action.node_id
        ).labels["rack"]
        self.assertIn(target_rack, stat.eligible_domains)

    def test_already_balanced_returns_empty_plan(self):
        nodes = (
            Node("n1", {"rack": "rk-a"}, free_slots=1),
            Node("n3", {"rack": "rk-b"}, free_slots=1),
        )
        replicas = (
            Replica("r1", NodeSelector(), current_node="n1"),
            Replica("r2", NodeSelector(), current_node="n3"),
        )
        rule = SpreadRule("rack-spread", "rack", max_skew=1)
        plan = find_plan(nodes, replicas, [rule], min_ready=2, max_surge=0)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.length, 0)
        self.assertEqual(plan.steps, ())
        self.assertEqual(plan.final_placement, ("n1", "n3"))


class MinReadyAndSurgeTest(unittest.TestCase):
    def test_stop_first_sequence_allowed_with_lower_min_ready(self):
        nodes, replicas, rule = _two_rack_stop_first_cluster()
        plan = find_plan(nodes, replicas, [rule], min_ready=3, max_surge=0)
        self.assertTrue(plan.feasible)
        # One replica must move rk-a -> rk-b: stop then start = 2 actions.
        self.assertEqual(plan.length, 2)
        # With no surge capacity the first action has to be a stop.
        self.assertEqual(plan.actions[0].kind, "stop")
        self.assertEqual(plan.actions[1].kind, "start")
        running_seen = [step.running_after for step in plan.steps]
        self.assertGreaterEqual(min(running_seen), 3)
        replay(PlannerInput(nodes, replicas, (rule,), 3, 0), plan)

    def test_no_surge_and_full_min_ready_is_infeasible(self):
        nodes, replicas, rule = _two_rack_stop_first_cluster()
        # min_ready equals the desired count and there is no surge: no start
        # slot exists and no stop is allowed -> deadlock.
        plan = find_plan(nodes, replicas, [rule], min_ready=4, max_surge=0)
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.actions, ())

    def test_running_count_never_exceeds_desired_plus_surge(self):
        nodes, replicas, rule = _four_rack_cluster()
        plan = find_plan(nodes, replicas, [rule], min_ready=4, max_surge=1)
        for step in plan.steps:
            self.assertLessEqual(step.running_after, 5)
            self.assertGreaterEqual(step.running_after, 4)


class ExcludedRackTest(unittest.TestCase):
    def test_excluded_rack_cannot_be_target_and_zero_min_path_works(self):
        # r2 excludes rk-a but is grandfathered there. rk-b already holds one.
        nodes = (
            Node("n1", {"rack": "rk-a"}, free_slots=0),
            Node("n2", {"rack": "rk-a"}, free_slots=0),
            Node("n3", {"rack": "rk-b"}, free_slots=1),
            Node("n4", {"rack": "rk-c"}, free_slots=2),
        )
        replicas = (
            Replica("r1", NodeSelector(), current_node="n3"),
            Replica("r2", NodeSelector(excluded={"rack": "rk-a"}), current_node="n2"),
        )
        rule = SpreadRule("rack-spread", "rack", max_skew=1)
        plan = find_plan(nodes, replicas, [rule], min_ready=1, max_surge=1)
        self.assertTrue(plan.feasible)

        # Every start must target an eligible node, never rk-a.
        for step in plan.steps:
            if step.action.kind == "start":
                target_rack = next(
                    n for n in nodes if n.node_id == step.action.node_id
                ).labels["rack"]
                self.assertNotEqual(target_rack, "rk-a")
                stat = step.domain_stats_before["rack-spread"]
                self.assertNotIn("rk-a", stat.eligible_domains)
                self.assertNotIn("rk-a", stat.replica_counts)
        replay(PlannerInput(nodes, replicas, (rule,), 1, 1), plan)

    def test_gate_behavior_differs_when_rack_wrongly_counted_as_zero(self):
        # With rk-a excluded, the eligible set is {rk-b, rk-c}; starting on
        # rk-c gives skew 1 (1-0) and is allowed. A wrong implementation that
        # lists rk-a as a zero-replica rack would corrupt the evidence even
        # though the allow/deny verdict happens to coincide here.
        nodes = (
            Node("n1", {"rack": "rk-a"}, free_slots=1),
            Node("n3", {"rack": "rk-b"}, free_slots=1),
            Node("n4", {"rack": "rk-c"}, free_slots=2),
        )
        picky = Replica("r2", NodeSelector(excluded={"rack": "rk-a"}))
        rule = SpreadRule("rack-spread", "rack", max_skew=1)
        placement = {"r2": frozenset(["n1"]), "r1": frozenset(["n3"])}
        gate = evaluate_start(nodes, placement, picky, nodes[2], rule)
        self.assertTrue(gate.allowed)
        # rk-a is absent: it must never appear, not even with a zero count.
        self.assertEqual(gate.replica_counts_after, {"rk-b": 1, "rk-c": 1})
        self.assertEqual(gate.eligible_domain_count, 2)

        # Same placement with an unrestricted selector: rk-a IS eligible and
        # already hosts one running instance, so its post-start count is 1.
        unrestricted = Replica("r2", NodeSelector())
        gate_full = evaluate_start(nodes, placement, unrestricted, nodes[2], rule)
        self.assertEqual(
            gate_full.replica_counts_after, {"rk-a": 1, "rk-b": 1, "rk-c": 1}
        )
        self.assertEqual(gate_full.eligible_domain_count, 3)
        # All three racks are eligible and rk-c was empty before the start,
        # so the effective minimum stays zero; the distinguishing evidence is
        # that rk-a is present here with count 1 but absent for the picky one.
        self.assertEqual(gate_full.effective_min, 0)


class EightNodeInterleavingTest(unittest.TestCase):
    def test_two_rules_interleaved_on_eight_nodes(self):
        # 8 nodes = 4 racks across 2 zones. Initial layout (2,2,0,0) on racks,
        # also (4,0) on zones: both rules observe skew 2 and must be satisfied
        # together on every start (the rules interleave within each action).
        nodes = tuple(
            [
                Node("n1", {"rack": "rk-a", "zone": "z1"}, free_slots=0),
                Node("n2", {"rack": "rk-a", "zone": "z1"}, free_slots=0),
                Node("n3", {"rack": "rk-b", "zone": "z2"}, free_slots=0),
                Node("n4", {"rack": "rk-b", "zone": "z2"}, free_slots=0),
                Node("n5", {"rack": "rk-c", "zone": "z1"}, free_slots=2),
                Node("n6", {"rack": "rk-c", "zone": "z1"}, free_slots=2),
                Node("n7", {"rack": "rk-d", "zone": "z2"}, free_slots=2),
                Node("n8", {"rack": "rk-d", "zone": "z2"}, free_slots=2),
            ]
        )
        replicas = (
            Replica("r1", NodeSelector(), current_node="n1"),
            Replica("r2", NodeSelector(), current_node="n2"),
            Replica("r3", NodeSelector(), current_node="n3"),
            Replica("r4", NodeSelector(), current_node="n4"),
        )
        rules = (
            SpreadRule("rack-spread", "rack", max_skew=1),
            SpreadRule("zone-spread", "zone", max_skew=1),
        )
        plan = find_plan(nodes, replicas, rules, min_ready=4, max_surge=1)
        self.assertTrue(plan.feasible)
        # Two replicas must move; start+stop each -> minimum 4 actions.
        self.assertEqual(plan.length, 4)
        placement = replay(
            PlannerInput(nodes, replicas, rules, 4, 1), plan
        )

        # Final layout: one replica per rack and two per zone (skew 0/1).
        used = [next(iter(v)) for v in placement.values()]
        label_of = {
            node_id: next(n for n in nodes if n.node_id == node_id).labels
            for node_id in used
        }
        rack_counts: dict[str, int] = {}
        zone_counts: dict[str, int] = {}
        for labels in label_of.values():
            rack_counts[labels["rack"]] = rack_counts.get(labels["rack"], 0) + 1
            zone_counts[labels["zone"]] = zone_counts.get(labels["zone"], 0) + 1
        self.assertEqual(rack_counts, {"rk-a": 1, "rk-b": 1, "rk-c": 1, "rk-d": 1})
        self.assertEqual(zone_counts, {"z1": 2, "z2": 2})

        # Every start step carries gate evidence for both rules.
        start_steps = [step for step in plan.steps if step.action.kind == "start"]
        self.assertEqual(len(start_steps), 2)
        for step in start_steps:
            self.assertEqual(set(step.gate_results), {"rack-spread", "zone-spread"})
            for gate in step.gate_results.values():
                self.assertTrue(gate.allowed)
            for rule_name in ("rack-spread", "zone-spread"):
                self.assertIn(rule_name, step.domain_stats_before)


class SlotAndValidationTest(unittest.TestCase):
    def test_node_without_free_slot_cannot_accept_start(self):
        nodes = (
            Node("n1", {"rack": "rk-a"}, free_slots=0),
            Node("n2", {"rack": "rk-b"}, free_slots=0),
            Node("n3", {"rack": "rk-c"}, free_slots=1),
        )
        replicas = (
            Replica("r1", NodeSelector(), current_node="n1"),
            Replica("r2", NodeSelector(), current_node="n2"),
        )
        rule = SpreadRule("rack-spread", "rack", max_skew=1)
        plan = find_plan(nodes, replicas, [rule], min_ready=2, max_surge=1)
        self.assertTrue(plan.feasible)
        for step in plan.steps:
            if step.action.kind == "start":
                self.assertEqual(step.action.node_id, "n3")
        replay(PlannerInput(nodes, replicas, (rule,), 2, 1), plan)

    def test_more_than_eight_nodes_rejected(self):
        nodes = tuple(
            Node(f"n{i}", {"rack": f"rk-{i % 3}"}, free_slots=1) for i in range(9)
        )
        replicas = (Replica("r1", NodeSelector(), current_node="n0"),)
        rule = SpreadRule("rack-spread", "rack", max_skew=1)
        with self.assertRaises(ValueError):
            Planner(PlannerInput(nodes, replicas, (rule,), 1, 0))

    def test_invalid_inputs_rejected(self):
        good = Node("n1", {"rack": "rk-a"}, free_slots=1)
        with self.assertRaises(ValueError):
            PlannerInput((), (Replica("r1", NodeSelector()),), (), 1, 0)
        with self.assertRaises(ValueError):
            PlannerInput(
                (good,),
                (Replica("r1", NodeSelector(), current_node="ghost"),),
                (),
                1,
                0,
            )
        with self.assertRaises(ValueError):
            NodeSelector(required={"k": "v"}, excluded={"k": "w"})
        with self.assertRaises(ValueError):
            SpreadRule("bad", "rack", max_skew=0)

    def test_actions_are_frozen_and_typed(self):
        action = Action("start", "r1", "n1")
        self.assertEqual((action.kind, action.replica_id, action.node_id),
                         ("start", "r1", "n1"))
        with self.assertRaises(ValueError):
            Action("evict", "r1", "n1")


if __name__ == "__main__":
    unittest.main()
