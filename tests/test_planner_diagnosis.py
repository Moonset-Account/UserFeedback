"""规划搜索的最优性、周转语义与不可退子集诊断。"""

from __future__ import annotations

import _bootstrap  # noqa: F401
import unittest

from safedrain.diagnosis import explain_irreducible
from safedrain.model import (
    Action,
    ExplorationLimit,
    PairType,
    PlanningError,
)
from safedrain.planner import (
    is_feasible,
    plan_for_retirement,
    plan_max_retirement,
    same_node_cliques,
)
from safedrain.simulate import physical_usage, replay_plan

from factories import build, node, pod, pair


def _retire_all_remaining_empty(plan, topo):
    events = replay_plan(topo, plan)
    final = events[-1].snapshot
    for i, p in enumerate(topo.pods):
        assert final.placement[i] not in plan.retiring


class PlannerBasicTests(unittest.TestCase):
    def test_already_empty_retirement_is_zero_moves(self):
        topo = build([node("n1"), node("n2")], [pod("p1", on="n2")])
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        self.assertEqual(plan.moves, 0)
        self.assertEqual(plan.actions, ())

    def test_simple_drain_minimum_moves(self):
        topo = build(
            [node("n1", cpu=2, mem=2), node("n2", cpu=2, mem=2)],
            [pod("p1", on="n1"), pod("p2", on="n1")],
        )
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        self.assertEqual(plan.moves, 2)
        self.assertTrue(all(a.source == "n1" for a in plan.actions))
        _retire_all_remaining_empty(plan, topo)

    def test_infeasible_when_no_capacity_anywhere(self):
        topo = build(
            [
                node("n1", cpu=2, mem=2),
                node("n2", cpu=1, mem=1),
            ],
            [pod("p1", on="n1"), pod("p2", on="n1")],
        )
        self.assertIsNone(plan_for_retirement(topo, frozenset({"n1"})))
        self.assertFalse(is_feasible(topo, frozenset({"n1"})))

    def test_immovable_pod_makes_node_irreducible(self):
        topo = build(
            [node("n1"), node("n2")],
            [pod("p1", on="n1", immovable=True)],
        )
        self.assertIsNone(plan_for_retirement(topo, frozenset({"n1"})))

    def test_cordoned_destination_means_no_plan(self):
        topo = build(
            [node("n1"), node("n2", cordoned=True)],
            [pod("p1", on="n1")],
        )
        self.assertIsNone(plan_for_retirement(topo, frozenset({"n1"})))


class TurnoverAndOptimalOrderTests(unittest.TestCase):
    def test_retained_node_used_as_temporary_turnover(self):
        # n1 要退，p1 需 2 格；n2 容量 2 但被原住 p2 占掉 1 格；
        # n3 容量只有 1，无法直接接纳 p1。于是唯一路径是先把 n2 的
        # 原住 p2 周转到 n3 腾出 n2，再让 p1: n1->n2。
        topo = build(
            [
                node("n1", cpu=2, mem=2),
                node("n2", cpu=2, mem=2),
                node("n3", cpu=1, mem=1),
            ],
            [
                pod("p1", on="n1", cpu=2, mem=2),
                pod("p2", on="n2", cpu=1, mem=1),
            ],
        )
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        self.assertIsNotNone(plan)
        # 至少有一张票据动过保留节点 n2 上的原住 Pod。
        moved_pods = {pid for a in plan.actions for pid in a.clique}
        self.assertIn("p2", moved_pods)
        # 最优搬迁次数：p2 周转一次 + p1 一次，共 2 次。
        self.assertEqual(plan.moves, 2)
        events = replay_plan(topo, plan)
        for event in events:
            for n in topo.nodes:
                self.assertTrue(
                    physical_usage(topo, event.snapshot, n.id).fits_in(n.capacity)
                )

    def test_lexicographically_smallest_action_sequence(self):
        # 两个对称目的地 n2/n3，字典序更小的 n2 应被优先选择。
        topo = build(
            [node("n1"), node("n2"), node("n3")],
            [pod("p1", on="n1")],
        )
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        self.assertEqual(plan.actions[0].destination, "n2")

    def test_min_moves_preferred_over_longer_chain(self):
        # n1->n2 直达可行；任何绕路方案都不应被选中。
        topo = build(
            [
                node("n1", cpu=2, mem=2),
                node("n2", cpu=2, mem=2),
                node("n3", cpu=2, mem=2),
            ],
            [pod("p1", on="n1"), pod("p2", on="n1")],
        )
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        self.assertEqual(plan.moves, 2)
        self.assertTrue(all(a.destination == "n2" for a in plan.actions))


class MaxRetirementTests(unittest.TestCase):
    def test_max_retirement_prefers_more_nodes(self):
        topo = build(
            [
                node("n1", cpu=1, mem=1),
                node("n2", cpu=1, mem=1),
                node("n3", cpu=4, mem=4),
            ],
            [pod("p1", on="n1"), pod("p2", on="n2")],
        )
        plan = plan_max_retirement(topo)
        self.assertEqual(plan.retiring, frozenset({"n1", "n2"}))
        _retire_all_remaining_empty(plan, topo)

    def test_max_retirement_falls_back_when_one_node_blocked(self):
        topo = build(
            [
                node("n1", cpu=1, mem=1),
                node("n2", cpu=1, mem=1),
                node("n3", cpu=1, mem=1),
            ],
            [
                pod("p1", on="n1", immovable=True),
                pod("p2", on="n2"),
            ],
        )
        plan = plan_max_retirement(topo)
        self.assertNotIn("n1", plan.retiring)
        self.assertIn("n2", plan.retiring)

    def test_max_retirement_tie_break_lexicographic(self):
        # n1、n2 都可退且规模相同，字典序更小的集合优先。
        topo = build(
            [
                node("n1", cpu=1, mem=1),
                node("n2", cpu=1, mem=1),
                node("n3", cpu=4, mem=4),
            ],
            [pod("p1", on="n1"), pod("p2", on="n2")],
        )
        plan = plan_max_retirement(topo, candidates={"n1", "n2"})
        self.assertEqual(plan.retiring, frozenset({"n1", "n2"}))


class DiagnosisTests(unittest.TestCase):
    def test_irreducible_core_with_witnesses(self):
        # 两台要退的节点各有一个 Pod；n3 容量恰好容纳一个。
        # 单独退 n1 或 n2 都可行（把那一个 Pod 放进 n3），
        # 同时退两台则 n3 装不下两个 -> {n1,n2} 是极小不可退核。
        topo = build(
            [
                node("n1", cpu=1, mem=1),
                node("n2", cpu=1, mem=1),
                node("n3", cpu=1, mem=1),
            ],
            [
                pod("p1", on="n1"),
                pod("p2", on="n2"),
            ],
        )
        core = explain_irreducible(topo, frozenset({"n1", "n2"}))
        self.assertEqual(core.core, frozenset({"n1", "n2"}))
        self.assertEqual(len(core.witnesses), 2)
        for witness in core.witnesses:
            self.assertEqual(witness.remaining, core.core - {witness.node})
            self.assertIsNotNone(witness.plan)
            replay_plan(topo, witness.plan)
        self.assertTrue(core.is_irreducible())

    def test_singleton_core_from_immovable_pod(self):
        # n1 上的 Pod 不可移动 -> {n1} 自身就是极小不可退核，
        # 移除唯一成员后得到空退役集（可行、零动作）。
        topo = build(
            [node("n1", cpu=1, mem=1), node("n2", cpu=1, mem=1)],
            [pod("p1", on="n1", immovable=True)],
        )
        core = explain_irreducible(topo, frozenset({"n1"}))
        self.assertEqual(core.core, frozenset({"n1"}))
        self.assertEqual(len(core.witnesses), 1)
        witness = core.witnesses[0]
        self.assertEqual(witness.node, "n1")
        self.assertEqual(witness.remaining, frozenset())
        self.assertEqual(witness.plan.moves, 0)

    def test_two_element_core_each_removal_feasible(self):
        # 两台要退的节点各有一个 Pod，但唯一接收节点 n3 只能装一个：
        # 单独退任意一台都可行，两台同时退不可行 -> 二元极小核。
        topo = build(
            [
                node("n1", cpu=1, mem=1),
                node("n2", cpu=1, mem=1),
                node("n3", cpu=1, mem=1),
            ],
            [pod("p1", on="n1"), pod("p2", on="n2")],
        )
        core = explain_irreducible(topo, frozenset({"n1", "n2"}))
        self.assertEqual(core.core, frozenset({"n1", "n2"}))
        self.assertEqual({w.node for w in core.witnesses}, {"n1", "n2"})
        for witness in core.witnesses:
            self.assertIsNotNone(witness.plan)
            replay_plan(topo, witness.plan)

    def test_explain_on_feasible_set_raises(self):
        topo = build([node("n1"), node("n2")], [pod("p1", on="n1")])
        with self.assertRaises(PlanningError):
            explain_irreducible(topo, frozenset({"n1"}))

    def test_larger_set_shrinks_to_singleton_core(self):
        # n3 上的 Pod 不可移动（n3 永不可退）；n1、n2 的 Pod 都能放进容量充足
        # 的 n4。{n1,n2,n3} 不可行，依次移除 n1、n2 后仍不可行，
        # 最终极小核为 {n3}，移除 n3 得到可行的空退役集。
        topo = build(
            [
                node("n1", cpu=1, mem=1),
                node("n2", cpu=1, mem=1),
                node("n3", cpu=1, mem=1),
                node("n4", cpu=4, mem=4),
            ],
            [
                pod("p1", on="n1"),
                pod("p2", on="n2"),
                pod("p3", on="n3", immovable=True),
            ],
        )
        core = explain_irreducible(topo, frozenset({"n1", "n2", "n3"}))
        self.assertEqual(core.core, frozenset({"n3"}))
        self.assertEqual(len(core.witnesses), 1)
        self.assertEqual(core.witnesses[0].remaining, frozenset())
        # 诊断结果确定：重复调用得到同一个核。
        again = explain_irreducible(topo, frozenset({"n1", "n2", "n3"}))
        self.assertEqual(again.core, core.core)


class BudgetTests(unittest.TestCase):
    def test_exploration_limit_raises(self):
        topo = build(
            [node("n1"), node("n2"), node("n3")],
            [pod("p1", on="n1"), pod("p2", on="n1")],
        )
        with self.assertRaises(ExplorationLimit):
            plan_for_retirement(topo, frozenset({"n1"}), max_states=0)


class CliqueTests(unittest.TestCase):
    def test_transitive_same_node_forms_one_clique(self):
        topo = build(
            [node("n1", cpu=4, mem=4), node("n2", cpu=4, mem=4)],
            [pod("p1", on="n1"), pod("p2", on="n1"), pod("p3", on="n1")],
            [
                pair("p1", "p2", PairType.SAME_NODE),
                pair("p2", "p3", PairType.SAME_NODE),
            ],
        )
        cliques = same_node_cliques(topo)
        self.assertEqual(cliques, [("p1", "p2", "p3")])


if __name__ == "__main__":
    unittest.main()
