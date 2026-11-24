import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest  # noqa: E402

from planner.model import Event, Problem  # noqa: E402
from planner.scheduler import (  # noqa: E402
    _HORIZON_SENTINEL,
    _Node,
    _apply_assign,
    plan,
)
from planner.verifier import verify_schedule, verify_small_projection  # noqa: E402

from tests.factories import W, make_temp, make_tenant, make_upstream, sl  # noqa: E402


def verify(case, problem, schedule):
    rep1 = verify_schedule(problem, schedule)
    case.assertTrue(rep1.ok, msg="; ".join(rep1.errors))
    rep2 = verify_small_projection(problem, schedule, W)
    case.assertTrue(rep2.ok, msg="; ".join(rep2.errors))


class TestInterleaving(unittest.TestCase):
    def _two_tenant_problem(self, u2_cap=8):
        u1 = make_upstream("U1", 0, 6, cap=8)
        u2 = make_upstream("U2", 0, 6, cap=u2_cap)
        t1 = make_tenant("t1", bits=2, old=(0, 3), old_upstream="U1", hold=2,
                         fixed=((6, 1),), allowed=("U2",))
        t2 = make_tenant("t2", bits=2, old=(16, 3), old_upstream="U1", hold=1,
                         allowed=("U2",))
        return u1, u2, t1, t2, Problem(upstreams=(u1, u2), tenants=(t1, t2))

    def test_second_hop_from_temp_state(self) -> None:
        # 手工构造两跳迁移的中间态：t1 已借用临时前缀、旧前缀已撤告，
        # 验证第二跳能关闭临时前缀并拿到最终前缀。
        u1, u2, t1, _, problem = self._two_tenant_problem()
        tmp = make_temp("TMP", 4, 2, "U1")
        temp_slot = sl(4, 2)
        node = _Node(
            active={"t1": [
                # 临时前缀在第 1 轮起开放持有。
                type("A", (), {
                    "tenant": "t1", "upstream": "U1", "prefix": temp_slot,
                    "temporary": True, "start": 1, "end": _HORIZON_SENTINEL,
                    "temp_name": "TMP", "open": True,
                })()
            ]},
            done=set(),
            events=[Event(1, "assign", "t1", "U1", temp_slot, True)],
            dual_used={"t1": 1},
            used_temp={"TMP"},
            next_round=2,
        )
        # 用真实 Alloc 重建，避免匿名对象缺字段。
        from planner.scheduler import Alloc
        node = _Node(
            active={"t1": [Alloc("t1", "U1", temp_slot, True, 1,
                                 _HORIZON_SENTINEL, "TMP")]},
            done=set(),
            events=[Event(1, "assign", "t1", "U1", temp_slot, True)],
            dual_used={"t1": 1},
            used_temp={"TMP"},
            next_round=2,
        )
        final_slot = sl(4, 2)  # 最终前缀（与临时槽位同坐标但标记为非临时）
        child = _apply_assign(
            problem, node, t1, final_slot, "U2", 2, 8,
            temporary=False, temp_name=None,
        )
        self.assertIsNotNone(child)
        withdraws = [e for e in child.events if e.kind == "withdraw" and e.tenant == "t1"]
        self.assertTrue(any(e.temporary for e in withdraws), "临时前缀必须被撤告")
        # 第二跳再产生 hold 个双地址轮，累计 2*hold。
        self.assertEqual(child.dual_used["t1"], 1 + t1.hold_rounds)

    def test_prefix_not_reused_before_withdrawal_complete(self) -> None:
        _, _, _, _, problem = self._two_tenant_problem()
        schedule = plan(problem)
        verify(self, problem, schedule)

        # 跨租户复用同一前缀时，后者的 assign 轮必须晚于前者撤告轮。
        spans: dict = {}
        for ev in schedule.events:
            if ev.kind != "assign":
                continue
            wd = next(
                (w for w in schedule.events
                 if w.kind == "withdraw" and w.tenant == ev.tenant and w.prefix == ev.prefix),
                None,
            )
            spans.setdefault(ev.prefix, []).append(
                (ev.tenant, ev.round, wd.round if wd else schedule.makespan + 1)
            )
        for pfx, entries in spans.items():
            owners = {e[0] for e in entries}
            if len(owners) > 1:
                ordered = sorted(entries, key=lambda e: e[1])
                for earlier, later in zip(ordered, ordered[1:]):
                    self.assertGreaterEqual(
                        later[1], earlier[2],
                        msg="前缀在撤告完成前被转给其他租户",
                    )

    def test_serial_dependency_under_tight_cap(self) -> None:
        # cap=2 允许单租户迁移（旧+新各一条），但两个租户同轮各占两条会超限，
        # 从而迫使至少部分动作错峰串行。
        u1, u2, t1, t2, _ = self._two_tenant_problem(u2_cap=2)
        problem = Problem(upstreams=(u1, u2), tenants=(t1, t2))
        schedule = plan(problem)
        verify(self, problem, schedule)
        for r in range(1, schedule.makespan + 1):
            count = sum(1 for a in schedule.advertisements
                        if a.round == r and a.upstream == "U2")
            self.assertLessEqual(count, 2)
        _, _, _, _, loose = self._two_tenant_problem(u2_cap=8)
        loose_schedule = plan(loose)
        self.assertGreaterEqual(schedule.makespan, loose_schedule.makespan)

    def test_temp_prefix_usage_and_release(self) -> None:
        # 提供临时前缀时，规划结果要么不借用（直接路径更优），要么借用后必撤告；
        # 两种情况都必须通过完整验证。
        u1, u2, t1, _, _ = self._two_tenant_problem()
        tmp = make_temp("TMP", 4, 2, "U1")
        problem = Problem(upstreams=(u1, u2), tenants=(t1,), temp_prefixes=(tmp,))
        schedule = plan(problem)
        verify(self, problem, schedule)
        temp_assigns = [e for e in schedule.events if e.temporary and e.kind == "assign"]
        for ev in temp_assigns:
            withdraw = next(
                (w for w in schedule.events
                 if w.kind == "withdraw" and w.tenant == ev.tenant and w.prefix == ev.prefix),
                None,
            )
            self.assertIsNotNone(withdraw, "借用的临时前缀必须最终撤告")
            self.assertGreaterEqual(withdraw.round, ev.round)


if __name__ == "__main__":
    unittest.main()
