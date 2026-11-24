import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest  # noqa: E402

from planner.aggregate import minimal_advertisements  # noqa: E402
from planner.model import (  # noqa: E402
    Advertisement,
    Event,
    Problem,
    Schedule,
)
from planner.prefix import parse_prefix  # noqa: E402
from planner.verifier import verify_schedule  # noqa: E402

from tests.factories import make_tenant, make_upstream  # noqa: E402


class TestLargeSparsePrefixes(unittest.TestCase):
    def test_huge_prefix_with_tiny_fixed_point_no_enumeration(self) -> None:
        # 真实 128 位前缀：/32 上游含 2^96 地址，固定点仅一个 /120，
        # 规划必须用区间端点运算完成，不能按地址数展开（性能上若展开会超时）。
        upstream = parse_prefix("2001:db8::/32")
        old = parse_prefix("2001:db8:1::/48")
        fixed = parse_prefix("2001:db8:1:2::/120")
        from planner.model import Tenant, Upstream
        tnt = Tenant(
            name="t",
            prefix_length=56,
            fixed=(fixed,),
            old_prefix=old,
            old_upstream="U",
            hold_rounds=2,
        )
        ups = Upstream("U", upstream, max_advertisements=4)
        problem = Problem(upstreams=(ups,), tenants=(tnt,))

        from planner.scheduler import plan
        schedule = plan(problem)
        rep = verify_schedule(problem, schedule)
        self.assertTrue(rep.ok, msg="; ".join(rep.errors))
        # 最终前缀是 /56 且包含 /120 固定点。
        placement = schedule.placements["t"]
        self.assertEqual(placement.length, 56)
        self.assertTrue(placement.contains(fixed))
        # 整个排程对象规模很小（不随 2^96 膨胀）。
        self.assertLess(len(schedule.advertisements), 20)

    def test_policy_labels_keep_subtrees_separate(self) -> None:
        a = parse_prefix("2001:db8::/65")
        b = a.sibling
        # 同租户不同策略：即使相邻也不能汇总。
        out = minimal_advertisements([(a, "t", "p1"), (b, "t", "p2")])
        self.assertEqual(len(out), 2)
        # 同租户同策略：兄弟合并为 /64。
        merged = minimal_advertisements([(a, "t", "p1"), (b, "t", "p1")])
        self.assertEqual(merged, [(parse_prefix("2001:db8::/64"), "t", "p1")])


class TestNegativeVerification(unittest.TestCase):
    def _good_schedule(self):
        u1 = make_upstream("U1", 0, 6, cap=8)
        u2 = make_upstream("U2", 0, 6, cap=8)
        t1 = make_tenant("t1", bits=2, old=(0, 3), old_upstream="U1", hold=2,
                         fixed=((6, 1),), allowed=("U2",))
        problem = Problem(upstreams=(u1, u2), tenants=(t1,))
        from planner.scheduler import plan
        return problem, plan(problem)

    def test_tampered_advertisement_declares_hole(self) -> None:
        problem, schedule = self._good_schedule()
        # 篡改通告：把某条前缀替换成覆盖空洞的更大前缀，验证器必须拒绝。
        ads = list(schedule.advertisements)
        bad = ads[0]
        bogus = Advertisement(
            bad.round, bad.upstream, make_upstream("X", 0, 6, 1).prefix,
            bad.tenant, bad.policy,
        )
        tampered = Schedule(
            events=schedule.events,
            advertisements=tuple([bogus] + ads[1:]),
            makespan=schedule.makespan,
            dual_address_rounds=schedule.dual_address_rounds,
            placements=schedule.placements,
        )
        rep = verify_schedule(problem, tampered)
        self.assertFalse(rep.ok)

    def test_missing_withdraw_detected(self) -> None:
        problem, schedule = self._good_schedule()
        events = tuple(e for e in schedule.events if e.kind != "withdraw")
        tampered = Schedule(
            events=events,
            advertisements=schedule.advertisements,
            makespan=schedule.makespan,
            dual_address_rounds=schedule.dual_address_rounds,
            placements=schedule.placements,
        )
        rep = verify_schedule(problem, tampered)
        self.assertFalse(rep.ok)
        self.assertTrue(any("撤告" in err for err in rep.errors))

    def test_cap_violation_rejected(self) -> None:
        # 构造一条超出上游上限的通告集，验证器必须报超限。
        problem, schedule = self._good_schedule()
        extra_prefix = make_upstream("E", 24, 3, 1).prefix
        extra = Advertisement(1, "U1", extra_prefix, "t1", "default")
        # 用极低下限重建问题。
        u1 = make_upstream("U1", 0, 6, cap=0)
        u2 = make_upstream("U2", 0, 6, cap=8)
        t1 = problem.tenants[0]
        tight = Problem(upstreams=(u1, u2), tenants=(t1,))
        rep = verify_schedule(tight, schedule)
        self.assertFalse(rep.ok)
        self.assertTrue(any("上限" in err for err in rep.errors))


if __name__ == "__main__":
    unittest.main()
