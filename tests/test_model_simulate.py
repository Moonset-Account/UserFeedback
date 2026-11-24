"""模型校验与两阶段模拟的正常、边界与约束场景。"""

from __future__ import annotations

import _bootstrap  # noqa: F401
import unittest

from safedrain.model import (
    Action,
    PairType,
    PlanningError,
    Resources,
)
from safedrain.simulate import (
    check_stable,
    physical_usage,
    replay_plan,
    reserve_action,
    release_action,
    simulate_action,
    Snapshot,
    initial_snapshot,
    zone_counts,
)

from factories import build, node, pod, pair


class ResourceModelTests(unittest.TestCase):
    def test_resources_arithmetic_and_fit(self):
        self.assertEqual(Resources(1, 2) + Resources(2, 1), Resources(3, 3))
        self.assertEqual(Resources(3, 3) - Resources(1, 2), Resources(2, 1))
        self.assertTrue(Resources(2, 2).fits_in(Resources(2, 3)))
        self.assertFalse(Resources(3, 1).fits_in(Resources(2, 2)))

    def test_negative_resources_rejected(self):
        with self.assertRaises(PlanningError):
            Resources(-1, 1)

    def test_duplicate_ids_and_unknown_home_rejected(self):
        with self.assertRaises(PlanningError):
            build([node("n1"), node("n1")], [pod("p1", on="n1")])
        with self.assertRaises(PlanningError):
            build([node("n1")], [pod("p1", on="ghost")])

    def test_pair_must_reference_known_pods(self):
        with self.assertRaises(PlanningError):
            build(
                [node("n1"), node("n2")],
                [pod("p1", on="n1"), pod("p2", on="n2")],
                [pair("p1", "ghost", PairType.SAME_NODE)],
            )


class CapacityAndIntermediateTests(unittest.TestCase):
    def _topo(self):
        # n1 容量恰好容纳两个 1cpu Pod；n2 只余 1cpu，必须先释放再接纳第二个。
        return build(
            [node("n1", cpu=2, mem=2), node("n2", cpu=2, mem=2)],
            [pod("p1", on="n1"), pod("p2", on="n1")],
        )

    def test_reserved_snapshot_double_counts_resource(self):
        topo = self._topo()
        snap = initial_snapshot(topo)
        action = Action("n1", "n2", ("p1",), "T1")
        events = reserve_action(topo, snap, action)
        reserved = events[-1].snapshot
        # 占位期间 p1 同时占用源与目的地：n1 仍有 p1+p2 共 2，n2 新增 p1 共 1。
        self.assertEqual(physical_usage(topo, reserved, "n1"), Resources(2, 2))
        self.assertEqual(physical_usage(topo, reserved, "n2"), Resources(1, 1))
        release_events = release_action(topo, reserved, action)
        final = release_events[-1].snapshot
        self.assertEqual(physical_usage(topo, final, "n1"), Resources(1, 1))
        self.assertEqual(physical_usage(topo, final, "n2"), Resources(1, 1))

    def test_reservation_rejected_when_double_occupancy_overflows(self):
        # n2 容量 1 且已被 p3 占满；p1 占位时 p3 仍在 n2，双计需要 2 -> 拒绝。
        topo = build(
            [node("n1", cpu=2, mem=2), node("n2", cpu=1, mem=1)],
            [pod("p1", on="n1"), pod("p2", on="n1"), pod("p3", on="n2")],
        )
        snap = initial_snapshot(topo)
        with self.assertRaises(PlanningError):
            reserve_action(topo, snap, Action("n1", "n2", ("p1",), "T1"))

    def test_sequential_moves_become_feasible_after_release(self):
        # 同上拓扑，但先把 n2 上的 p3 挪走后，p1/p2 可依次进入 n2。
        topo = build(
            [
                node("n1", cpu=2, mem=2),
                node("n2", cpu=2, mem=2),
                node("n3", cpu=2, mem=2),
            ],
            [
                pod("p1", on="n1"),
                pod("p2", on="n1"),
                pod("p3", on="n2"),
            ],
        )
        from safedrain.planner import plan_for_retirement

        plan = plan_for_retirement(topo, frozenset({"n1"}))
        self.assertIsNotNone(plan)
        events = replay_plan(topo, plan)
        # 终态 n1 无残留，且每个中间快照都不超配。
        for event in events:
            for n in topo.nodes:
                self.assertTrue(
                    physical_usage(topo, event.snapshot, n.id).fits_in(n.capacity),
                    f"中间态超配：{event}",
                )
        final_placement = events[-1].snapshot.placement
        self.assertNotIn("n1", final_placement)


class CordonAndSelectorTests(unittest.TestCase):
    def test_cordoned_node_rejects_new_replica_but_keeps_native(self):
        topo = build(
            [
                node("n1", cpu=2, mem=2),
                node("n2", cpu=2, mem=2, cordoned=True),
                node("n3", cpu=2, mem=2),
            ],
            [pod("p1", on="n1"), pod("p2", on="n2")],
        )
        snap = initial_snapshot(topo)
        with self.assertRaises(PlanningError):
            reserve_action(topo, snap, Action("n1", "n2", ("p1",), "T1"))
        # 软下线节点上的原住 Pod 仍可搬到别处。
        events = simulate_action(topo, snap, Action("n2", "n3", ("p2",), "T1"))
        self.assertEqual(events[-1].snapshot.placement[1], "n3")

    def test_selector_must_match_destination_labels(self):
        topo = build(
            [
                node("n1", cpu=2, mem=2, labels=(("gpu", "true"),)),
                node("n2", cpu=2, mem=2, labels=(("gpu", "true"),)),
                node("n3", cpu=2, mem=2),
            ],
            [pod("p1", on="n1", selector=(("gpu", "true"),))],
        )
        snap = initial_snapshot(topo)
        with self.assertRaises(PlanningError):
            reserve_action(topo, snap, Action("n1", "n3", ("p1",), "T1"))
        events = simulate_action(topo, snap, Action("n1", "n2", ("p1",), "T1"))
        self.assertEqual(events[-1].snapshot.placement[0], "n2")

    def test_immovable_pod_blocks_action(self):
        topo = build(
            [node("n1"), node("n2")],
            [pod("p1", on="n1", immovable=True)],
        )
        with self.assertRaises(PlanningError):
            simulate_action(
                topo, initial_snapshot(topo), Action("n1", "n2", ("p1",), "T1")
            )


class PairConstraintTests(unittest.TestCase):
    def test_different_node_pair_rejects_collision_including_reserved(self):
        topo = build(
            [node("n1"), node("n2"), node("n3")],
            [pod("p1", on="n1"), pod("p2", on="n2")],
            [pair("p1", "p2", PairType.DIFFERENT_NODE)],
        )
        snap = initial_snapshot(topo)
        with self.assertRaises(PlanningError):
            simulate_action(topo, snap, Action("n1", "n2", ("p1",), "T1"))

    def test_same_node_clique_moves_together(self):
        topo = build(
            [node("n1", cpu=4, mem=4), node("n2", cpu=4, mem=4)],
            [pod("p1", on="n1"), pod("p2", on="n1")],
            [pair("p1", "p2", PairType.SAME_NODE)],
        )
        from safedrain.planner import plan_for_retirement, same_node_cliques

        cliques = same_node_cliques(topo)
        self.assertIn(("p1", "p2"), cliques)
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(plan.actions[0].clique, ("p1", "p2"))
        events = replay_plan(topo, plan)
        # 占位阶段两个 Pod 同时在途，稳定后同机。
        reserved = [e for e in events if e.kind == "reserved"]
        self.assertEqual(len(reserved), 2)
        check_stable(topo, events[-1].snapshot.placement)


class ZoneDeltaTests(unittest.TestCase):
    def test_in_flight_replica_remains_in_origin_zone(self):
        # 初始 z1=2、z2=0，cap=1：零副本区域也纳入比较，初始态即非法，
        # initial_snapshot 必须拒绝（而不是等到第一次搬迁才发现）。
        topo = build(
            [
                node("n1", zone="z1", cpu=4, mem=4),
                node("n2", zone="z2", cpu=4, mem=4),
            ],
            [pod("p1", on="n1", workload="w"), pod("p2", on="n1", workload="w")],
            caps={"w": 1},
        )
        with self.assertRaises(PlanningError):
            initial_snapshot(topo)

    def test_zone_delta_checked_after_each_confirmation(self):
        # 初始 z1=2、z2=1，cap=1。搬第一个后 z1=1、z2=2（差1，合法）；
        # 若再搬第二个则 z1=0、z2=3（差3，非法），规划器必须找不到两连搬。
        topo = build(
            [
                node("n1", zone="z1", cpu=4, mem=4),
                node("n2", zone="z2", cpu=4, mem=4),
                node("n3", zone="z1", cpu=4, mem=4),
            ],
            [
                pod("p1", on="n1", workload="w"),
                pod("p2", on="n1", workload="w"),
                pod("p3", on="n2", workload="w"),
            ],
            caps={"w": 1},
        )
        snap = initial_snapshot(topo)
        action = Action("n1", "n2", ("p1",), "T1")
        events = simulate_action(topo, snap, action)
        counts = zone_counts(topo, events[-1].snapshot, "w")
        self.assertEqual(counts, {"z1": 1, "z2": 2})

        # 第二搬在释放确认时违反区域差，必须抛错。
        with self.assertRaises(PlanningError):
            simulate_action(
                topo, events[-1].snapshot, Action("n1", "n2", ("p2",), "T2")
            )

    def test_reservation_keeps_origin_zone_counts(self):
        # 三区各一副本、cap=0 的合法初始态；p1 从 z1 到 z2 占位期间仍归 z1，
        # 区域计数保持 {z1:1, z2:1, z3:1}。
        topo = build(
            [
                node("n1", zone="z1", cpu=4, mem=4),
                node("n2", zone="z2", cpu=4, mem=4),
                node("n3", zone="z3", cpu=4, mem=4),
            ],
            [
                pod("p1", on="n1", workload="w"),
                pod("p2", on="n2", workload="w"),
                pod("p3", on="n3", workload="w"),
            ],
            caps={"w": 0},
        )
        snap = initial_snapshot(topo)
        reserved = reserve_action(topo, snap, Action("n1", "n2", ("p1",), "T1"))[-1].snapshot
        # 占位期间 p1 仍归 z1，区域差保持 0。
        self.assertEqual(
            zone_counts(topo, reserved, "w"), {"z1": 1, "z2": 1, "z3": 1}
        )


if __name__ == "__main__":
    unittest.main()
