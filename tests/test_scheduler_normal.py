import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest  # noqa: E402

from planner.model import Problem  # noqa: E402
from planner.scheduler import plan  # noqa: E402
from planner.verifier import verify_schedule, verify_small_projection  # noqa: E402

from tests.factories import W, make_tenant, make_upstream, sl  # noqa: E402


def assert_fully_verified(case: unittest.TestCase, problem: Problem, schedule) -> None:
    symbolic = verify_schedule(problem, schedule)
    case.assertTrue(symbolic.ok, msg="; ".join(symbolic.errors))
    projected = verify_small_projection(problem, schedule, W)
    case.assertTrue(projected.ok, msg="; ".join(projected.errors))


class TestNormalMigration(unittest.TestCase):
    def test_single_tenant_fixed_point_renumber(self) -> None:
        # 旧前缀 [0,8)；新前缀更细（4 单位），槽位 [4,8) 覆盖固定点 [6,8)，
        # 且与旧前缀不同，构成真正换号。
        u1 = make_upstream("U1", 0, 6, cap=4)
        u2 = make_upstream("U2", 0, 5, cap=4)
        t1 = make_tenant(
            "t1", bits=2, old=(0, 3), old_upstream="U1", hold=2,
            fixed=((6, 1),), allowed=("U2",),
        )
        problem = Problem(upstreams=(u1, u2), tenants=(t1,))
        schedule = plan(problem)
        assert_fully_verified(self, problem, schedule)
        self.assertEqual(schedule.makespan, 2)
        self.assertEqual(schedule.dual_address_rounds, 2)
        self.assertTrue(schedule.placements["t1"].contains(sl(6, 1)))
        self.assertNotEqual(schedule.placements["t1"], t1.old_prefix)
        self.assertTrue(u2.prefix.contains(schedule.placements["t1"]))

    def test_two_tenants_parallel_migration(self) -> None:
        # 两个租户可在同一视界内并行迁移；hold 不同，取最大 hold 决定 makespan。
        u1 = make_upstream("U1", 0, 6, cap=8)
        u2 = make_upstream("U2", 0, 5, cap=8)
        t1 = make_tenant(
            "t1", bits=2, old=(0, 3), old_upstream="U1", hold=2,
            fixed=((6, 1),), allowed=("U2",),
        )
        t2 = make_tenant(
            "t2", bits=2, old=(8, 3), old_upstream="U1", hold=1,
            fixed=((14, 1),), allowed=("U2",),
        )
        problem = Problem(upstreams=(u1, u2), tenants=(t1, t2))
        schedule = plan(problem)
        assert_fully_verified(self, problem, schedule)
        self.assertEqual(schedule.makespan, 2)
        self.assertEqual(schedule.dual_address_rounds, 2 + 1)
        # 两租户最终前缀互不相交。
        p1, p2 = schedule.placements["t1"], schedule.placements["t2"]
        from planner.prefix import intervals_disjoint
        self.assertTrue(intervals_disjoint(p1.interval, p2.interval))

    def test_every_round_has_minimal_advertisements(self) -> None:
        u1 = make_upstream("U1", 0, 6, cap=8)
        u2 = make_upstream("U2", 0, 5, cap=8)
        t1 = make_tenant(
            "t1", bits=2, old=(0, 3), old_upstream="U1", hold=2,
            fixed=((6, 1),), allowed=("U2",),
        )
        problem = Problem(upstreams=(u1, u2), tenants=(t1,))
        schedule = plan(problem)
        assert_fully_verified(self, problem, schedule)
        for r in range(1, schedule.makespan + 1):
            self.assertTrue(
                any(a.round == r for a in schedule.advertisements),
                msg=f"第 {r} 轮缺少通告",
            )


if __name__ == "__main__":
    unittest.main()
