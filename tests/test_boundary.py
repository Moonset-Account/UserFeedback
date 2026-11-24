import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest  # noqa: E402

from planner.model import Problem, Prefix, Tenant, Upstream  # noqa: E402
from planner.scheduler import NoFeasibleSchedule, plan  # noqa: E402
from planner.verifier import verify_schedule, verify_small_projection  # noqa: E402

from tests.factories import W, make_tenant, make_upstream, sl  # noqa: E402


def verify(case, problem, schedule):
    rep1 = verify_schedule(problem, schedule)
    case.assertTrue(rep1.ok, msg="; ".join(rep1.errors))
    rep2 = verify_small_projection(problem, schedule, W)
    case.assertTrue(rep2.ok, msg="; ".join(rep2.errors))


class TestBoundary(unittest.TestCase):
    def test_hold_one_single_round_dual(self) -> None:
        u1 = make_upstream("U1", 0, 6, cap=8)
        u2 = make_upstream("U2", 0, 5, cap=8)
        t1 = make_tenant(
            "t1", bits=2, old=(0, 3), old_upstream="U1", hold=1,
            fixed=((6, 1),), allowed=("U2",),
        )
        problem = Problem(upstreams=(u1, u2), tenants=(t1,))
        schedule = plan(problem)
        verify(self, problem, schedule)
        # hold=1：第 1 轮双地址并在该轮末撤告，总轮数 1。
        self.assertEqual(schedule.makespan, 1)
        self.assertEqual(schedule.dual_address_rounds, 1)

    def test_finest_granularity_single_unit_tenant(self) -> None:
        # 新前缀仅占 1 个投影地址单位（投影 /8，真实对应最小可表达块）。
        u1 = make_upstream("U1", 0, 6, cap=8)
        u2 = make_upstream("U2", 0, 5, cap=8)
        t1 = make_tenant(
            "t1", bits=0, old=(0, 3), old_upstream="U1", hold=1,
            fixed=((7, 0),), allowed=("U2",),
        )
        problem = Problem(upstreams=(u1, u2), tenants=(t1,))
        schedule = plan(problem)
        verify(self, problem, schedule)
        self.assertEqual(schedule.placements["t1"].size, sl(7, 0).size)

    def test_eight_tenants_limit(self) -> None:
        u1 = make_upstream("U1", 0, 6, cap=32)
        u2 = make_upstream("U2", 0, 6, cap=32)
        tenants = []
        # 8 个租户，旧前缀占据 [0,8),[8,16)...，固定点放在各自旧前缀末端单位。
        for i in range(8):
            old_addr = i  # 每个旧前缀 8 单位，起点 i*8
            tenants.append(
                make_tenant(
                    f"t{i}",
                    bits=2,
                    old=(i * 8, 3),
                    old_upstream="U1",
                    hold=1,
                    fixed=((i * 8 + 6, 1),),
                    allowed=("U2",),
                )
            )
        problem = Problem(upstreams=(u1, u2), tenants=tuple(tenants))
        schedule = plan(problem)
        verify(self, problem, schedule)
        self.assertEqual(len(schedule.placements), 8)
        # 所有最终前缀两两不相交。
        from planner.prefix import intervals_disjoint
        pfxs = list(schedule.placements.values())
        for i, a in enumerate(pfxs):
            for b in pfxs[i + 1 :]:
                self.assertTrue(intervals_disjoint(a.interval, b.interval))

    def test_nine_tenants_rejected(self) -> None:
        u1 = make_upstream("U1", 0, 6, cap=64)
        tenants = tuple(
            make_tenant(f"t{i}", bits=0, old=(i, 0), old_upstream="U1", hold=1)
            for i in range(9)
        )
        with self.assertRaises(ValueError):
            Problem(upstreams=(u1,), tenants=tenants)

    def test_infeasible_when_no_slot_covers_fixed_point(self) -> None:
        u1 = make_upstream("U1", 0, 6, cap=8)
        # U2 只覆盖 [0,16)，但固定点 [14,16) 的唯一非旧槽位在 U2 之外不可达。
        u2 = make_upstream("U2", 0, 3, cap=8)
        t1 = make_tenant(
            "t1", bits=2, old=(8, 3), old_upstream="U1", hold=1,
            fixed=((14, 1),), allowed=("U2",),
        )
        problem = Problem(upstreams=(u1, u2), tenants=(t1,))
        with self.assertRaises(NoFeasibleSchedule):
            plan(problem)

    def test_input_validation(self) -> None:
        with self.assertRaises(ValueError):
            Prefix(1, 64)  # 未对齐
        with self.assertRaises(ValueError):
            Tenant("x", prefix_length=200)
        with self.assertRaises(ValueError):
            Tenant("x", prefix_length=64, hold_rounds=0)
        good = Upstream("u", Prefix(0, 0), 1)
        with self.assertRaises(ValueError):
            Upstream("u", Prefix(0, 0), -1)
        # 固定点不在旧前缀内拒绝。
        with self.assertRaises(ValueError):
            Tenant(
                "x",
                prefix_length=126,
                fixed=(Prefix(0, 128),),
                old_prefix=Prefix(8, 126),
                old_upstream="u",
            )


if __name__ == "__main__":
    unittest.main()
