"""综合场景：规模边界、多约束交错与端到端可恢复链路。"""

from __future__ import annotations

import _bootstrap  # noqa: F401
import unittest

from safedrain.controller import DrainController
from safedrain.diagnosis import explain_irreducible
from safedrain.model import (
    MAX_NODES,
    MAX_PODS,
    PairType,
    PlanningError,
)
from safedrain.planner import plan_for_retirement, plan_max_retirement
from safedrain.simulate import (
    physical_usage,
    replay_plan,
    zone_counts,
)

from factories import build, node, pod, pair


class ScaleBoundaryTests(unittest.TestCase):
    def test_max_10_nodes_18_pods_topology_plans(self):
        # 达到题目规模上限的拓扑：10 节点、18 Pod。只对单个退役集合做 BFS，
        # 避免最大退役枚举在大状态空间上的组合爆炸；预算固定保证有界。
        nodes = [node(f"n{i}", cpu=8, mem=8, zone=f"z{i % 3}")
                 for i in range(MAX_NODES)]
        pods = [
            pod(f"p{i}", workload="w", on=f"n{i % 9}")
            for i in range(MAX_PODS)
        ]
        topo = build(nodes, pods, caps={"w": 8})
        plan = plan_for_retirement(
            topo, frozenset({"n0"}), max_states=20_000, max_actions=100_000
        )
        self.assertIsNotNone(plan)
        events = replay_plan(topo, plan)
        final = events[-1].snapshot
        for i, p in enumerate(topo.pods):
            self.assertNotIn(final.placement[i], plan.retiring)
        # 所有中间快照都不超配。
        for event in events:
            for n in topo.nodes:
                self.assertTrue(
                    physical_usage(topo, event.snapshot, n.id).fits_in(n.capacity)
                )

    def test_beyond_limits_rejected(self):
        from safedrain.model import Topology, Resources, Node as MNode, Pod as MPod

        with self.assertRaises(PlanningError):
            Topology(
                nodes=[MNode(f"n{i}", Resources(1, 1), "z")
                       for i in range(MAX_NODES + 1)],
                pods=[MPod("p0", "w", Resources(1, 1), node="n0")],
            )
        with self.assertRaises(PlanningError):
            Topology(
                nodes=[MNode("n0", Resources(20, 20), "z")],
                pods=[MPod(f"p{i}", "w", Resources(1, 1), node="n0")
                      for i in range(MAX_PODS + 1)],
            )


class InterleavedConstraintsTests(unittest.TestCase):
    def test_same_and_different_node_pairs_interleave(self):
        # p1,p2 必须同机；它们整体与 p3 必须异机。退 n1 时整组搬到 n3，
        # 且 n3 上不能已有 p3（因此 p3 需先让到 n4）。
        topo = build(
            [
                node("n1", cpu=4, mem=4),
                node("n2", cpu=4, mem=4),
                node("n3", cpu=4, mem=4),
                node("n4", cpu=4, mem=4),
            ],
            [
                pod("p1", on="n1"),
                pod("p2", on="n1"),
                pod("p3", on="n3"),
            ],
            [
                pair("p1", "p2", PairType.SAME_NODE),
                pair("p1", "p3", PairType.DIFFERENT_NODE),
            ],
        )
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        self.assertIsNotNone(plan)
        events = replay_plan(topo, plan)
        final = events[-1].snapshot
        pos = {p.id: final.placement[i] for i, p in enumerate(topo.pods)}
        self.assertEqual(pos["p1"], pos["p2"])
        self.assertNotEqual(pos["p1"], pos["p3"])
        self.assertNotIn("n1", final.placement)

    def test_zone_cap_plus_cordon_plus_immovable_together(self):
        # n1（z1）要退；n2（z2）软下线不能接收；n3（z1）可接收但区域差
        # cap=0 要求两区始终相等；p4 不可移动钉在 n3。
        topo = build(
            [
                node("n1", zone="z1", cpu=4, mem=4),
                node("n2", zone="z2", cpu=4, mem=4, cordoned=True),
                node("n3", zone="z1", cpu=4, mem=4),
                node("n4", zone="z2", cpu=4, mem=4),
            ],
            [
                pod("p1", on="n1", workload="w"),
                pod("p2", on="n1", workload="w"),
                pod("p3", on="n4", workload="w"),
                pod("p4", on="n3", workload="w", immovable=True),
            ],
            caps={"w": 1},
        )
        # 初始 z1=3（n1 两个 + n3 一个）、z2=1（n4 一个），差 2 > 1，非法。
        from safedrain.simulate import initial_snapshot

        with self.assertRaises(PlanningError):
            initial_snapshot(topo)

        # 放宽 cap 到 2 后，p1/p2 只能去 n3（n2 软下线），方案应可行。
        topo2 = build(topo.nodes, topo.pods, topo.pairs, caps={"w": 2})
        plan = plan_for_retirement(topo2, frozenset({"n1"}))
        self.assertIsNotNone(plan)
        events = replay_plan(topo2, plan)
        for event in events:
            self.assertLessEqual(
                max(zone_counts(topo2, event.snapshot, "w").values())
                - min(zone_counts(topo2, event.snapshot, "w").values()),
                2,
            )


class EndToEndRecoveryTests(unittest.TestCase):
    def test_full_drain_with_two_controller_switches(self):
        topo = build(
            [
                node("n1", cpu=2, mem=2),
                node("n2", cpu=2, mem=2),
                node("n3", cpu=2, mem=2),
            ],
            [pod("p1", on="n1"), pod("p2", on="n1")],
        )
        plan = plan_for_retirement(topo, frozenset({"n1"}))

        # 第一次切换：第一张票据占位之后。
        c1 = DrainController(topo, plan)
        c1.reserve_next()
        c2 = DrainController.from_json(topo, plan, c1.to_json())
        c2.confirm_release()

        # 第二次切换：第二张票据占位之后。
        c2.reserve_next()
        c3 = DrainController.from_json(topo, plan, c2.to_json())
        c3.confirm_release()

        self.assertTrue(c3.finished)
        # 与无中断回放的终态完全一致。
        expected_final = replay_plan(topo, plan)[-1].snapshot.placement
        self.assertEqual(c3.current_snapshot.placement, expected_final)
        self.assertNotIn("n1", c3.current_snapshot.placement)

    def test_diagnosis_witness_plan_is_replayable(self):
        topo = build(
            [
                node("n1", cpu=1, mem=1),
                node("n2", cpu=1, mem=1),
                node("n3", cpu=1, mem=1),
            ],
            [pod("p1", on="n1"), pod("p2", on="n2")],
        )
        core = explain_irreducible(topo, frozenset({"n1", "n2"}))
        for witness in core.witnesses:
            events = replay_plan(topo, witness.plan)
            for node_id in witness.remaining:
                self.assertNotIn(node_id, events[-1].snapshot.placement)


if __name__ == "__main__":
    unittest.main()
