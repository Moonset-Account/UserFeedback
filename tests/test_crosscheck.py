"""Cross-checks that do not rely on the planner internals.

- Re-simulate every produced plan against independently recomputed gates.
- Verify BFS minimality by enumerating action sequences up to plan.length-1.
- Time the worst supported size (8 nodes).
"""

import time
import unittest

from rolling_relayout import (
    Node,
    NodeSelector,
    Replica,
    SpreadRule,
    evaluate_domains,
    evaluate_start,
    find_plan,
)


def _independently_validate(nodes, replicas, rules, min_ready, max_surge, plan):
    node_by_id = {n.node_id: n for n in nodes}
    replica_by_id = {r.replica_id: r for r in replicas}
    free = {n.node_id: n.free_slots for n in nodes}
    running = {
        r.replica_id: ({r.current_node} if r.current_node else set())
        for r in replicas
    }
    cap = len(replicas) + max_surge

    for action in plan.actions:
        replica = replica_by_id[action.replica_id]
        node = node_by_id[action.node_id]
        total = sum(len(v) for v in running.values())
        placement = {rid: frozenset(v) for rid, v in running.items()}
        if action.kind == "start":
            assert free[node.node_id] > 0
            assert replica.selector.matches(node)
            assert total + 1 <= cap
            for rule in rules:
                assert evaluate_start(nodes, placement, replica, node, rule).allowed
            free[node.node_id] -= 1
            running[action.replica_id].add(node.node_id)
        else:
            assert total - 1 >= min_ready
            assert node.node_id in running[action.replica_id]
            free[node.node_id] += 1
            running[action.replica_id].discard(node.node_id)

    assert all(len(v) == 1 for v in running.values())
    placement = {rid: frozenset(v) for rid, v in running.items()}
    for replica in replicas:
        final_node = node_by_id[next(iter(running[replica.replica_id]))]
        assert replica.selector.matches(final_node)
        for rule in rules:
            stat = evaluate_domains(nodes, placement, replica, rule)
            assert stat.max_skew_observed <= rule.max_skew


def _brute_force_min_depth(nodes, replicas, rules, min_ready, max_surge, limit):
    """BFS implemented independently with explicit (node_id, kind) tuples."""

    node_by_id = {n.node_id: n for n in nodes}
    initial = tuple(
        frozenset([r.current_node]) if r.current_node else frozenset()
        for r in replicas
    )
    cap = len(replicas) + max_surge

    def free_slots(state, node_id, initial_occ):
        on_node = sum(1 for s in state if node_id in s)
        return node_by_id[node_id].free_slots - (on_node - initial_occ[node_id])

    def goal(state):
        if any(len(s) != 1 for s in state):
            return False
        placement = {replicas[i].replica_id: state[i] for i in range(len(replicas))}
        for i, replica in enumerate(replicas):
            node = node_by_id[next(iter(state[i]))]
            if not replica.selector.matches(node):
                return False
        for replica in replicas:
            for rule in rules:
                if evaluate_domains(nodes, placement, replica, rule).max_skew_observed > rule.max_skew:
                    return False
        return True

    initial_occ = {n.node_id: 0 for n in nodes}
    for r in replicas:
        if r.current_node:
            initial_occ[r.current_node] += 1

    frontier = [initial]
    seen = {initial}
    for depth in range(limit):
        next_frontier = []
        for state in frontier:
            total = sum(len(s) for s in state)
            placement = {replicas[i].replica_id: state[i] for i in range(len(replicas))}
            candidates = []
            if total < cap:
                for i, replica in enumerate(replicas):
                    for node in nodes:
                        if node.node_id in state[i]:
                            continue
                        if free_slots(state, node.node_id, initial_occ) <= 0:
                            continue
                        if not replica.selector.matches(node):
                            continue
                        if all(
                            evaluate_start(nodes, placement, replica, node, rule).allowed
                            for rule in rules
                        ):
                            new = list(state)
                            new[i] = frozenset(set(state[i]) | {node.node_id})
                            candidates.append(tuple(new))
            if total > min_ready:
                for i in range(len(replicas)):
                    for node_id in state[i]:
                        new = list(state)
                        new[i] = frozenset(set(state[i]) - {node_id})
                        candidates.append(tuple(new))
            for candidate in candidates:
                if candidate in seen:
                    continue
                if goal(candidate):
                    return depth + 1
                seen.add(candidate)
                next_frontier.append(candidate)
        frontier = next_frontier
        if not frontier:
            return None
    return None


class CrossCheckTest(unittest.TestCase):
    def test_planner_matches_independent_brute_force_depth(self):
        nodes = (
            Node("n1", {"rack": "rk-a"}, free_slots=0),
            Node("n2", {"rack": "rk-a"}, free_slots=0),
            Node("n3", {"rack": "rk-b"}, free_slots=0),
            Node("n4", {"rack": "rk-b"}, free_slots=0),
            Node("n5", {"rack": "rk-c"}, free_slots=2),
        )
        replicas = (
            Replica("r1", NodeSelector(), current_node="n1"),
            Replica("r2", NodeSelector(), current_node="n2"),
            Replica("r3", NodeSelector(), current_node="n3"),
        )
        rule = SpreadRule("rack-spread", "rack", max_skew=1)
        plan = find_plan(nodes, replicas, [rule], min_ready=3, max_surge=1)
        self.assertTrue(plan.feasible)
        _independently_validate(nodes, replicas, [rule], 3, 1, plan)
        # No goal may exist at any strictly smaller depth.
        shorter = _brute_force_min_depth(nodes, replicas, [rule], 3, 1, plan.length - 1)
        self.assertIsNone(shorter)
        # The independent search reaches a goal exactly at the planner depth.
        same = _brute_force_min_depth(nodes, replicas, [rule], 3, 1, plan.length)
        self.assertEqual(same, plan.length)

    def test_eight_node_search_runs_quickly(self):
        # 4 nodes per zone; all six initial instances live in z0 (z0=6, z1=0),
        # two per rack on rk-0..rk-2: both rules start with large skews and
        # the search must interleave their gate checks on every start action.
        nodes = tuple(
            Node(
                f"n{i}",
                {"rack": f"rk-{i // 2}", "zone": "z0" if i < 4 else "z1"},
                free_slots=2,
            )
            for i in range(8)
        )
        replicas = tuple(
            Replica(f"r{i}", NodeSelector(), current_node=f"n{i}") for i in range(6)
        )
        rules = (
            SpreadRule("rack-spread", "rack", max_skew=1),
            SpreadRule("zone-spread", "zone", max_skew=1),
        )
        start = time.perf_counter()
        plan = find_plan(nodes, replicas, rules, min_ready=6, max_surge=1)
        elapsed = time.perf_counter() - start
        self.assertTrue(plan.feasible)
        self.assertGreater(plan.length, 0)
        _independently_validate(nodes, replicas, rules, 6, 1, plan)
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main()
