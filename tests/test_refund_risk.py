"""退款风险判定组件验收用例（unittest）。

覆盖题面交叉约束：
- 按退款业务编号合并重试（含不一致重试被拒绝）；
- 累计实际退款阈值的可接受 / 被阻止结果与等值边界；
- 不同订单共享收款凭据时的累计拦截，挂起待决不计入累计；
- 已确认拒付条件（凭据关联 + 订单匹配、可配置拒付笔数）；
- 拒付迟到：只重算相关待决退款，已执行保留原决定并生成复核信号；
- 凭据经商户确认替换后，旧凭据风险记录仅在交接期内关联（含等值边界、
  未确认替换不关联）；
- 多事件交错场景。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from refund_risk import (
    Chargeback,
    Credential,
    Decision,
    Reason,
    RefundRequest,
    RefundRiskEngine,
    RefundStatus,
    RiskConfig,
)

T = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def req(
    refund_no: str,
    order_id: str,
    credential_id: str,
    amount: str,
    at: datetime,
) -> RefundRequest:
    return RefundRequest(
        refund_no=refund_no,
        order_id=order_id,
        credential_id=credential_id,
        amount=Decimal(amount),
        requested_at=at,
    )


def make_engine(limit: str = "100", chargeback_limit: int = 1,
                window: timedelta = timedelta(days=7)) -> RefundRiskEngine:
    return RefundRiskEngine(
        RiskConfig(
            cumulative_refund_limit=Decimal(limit),
            chargeback_limit=chargeback_limit,
            handover_window=window,
        )
    )


class IdempotencyMergeTest(unittest.TestCase):
    """同一退款业务编号的批量重试必须被合并为一笔。"""

    def test_retries_merged_into_single_refund(self):
        engine = make_engine(limit="100")
        first = engine.evaluate(req("R1", "O1", "C1", "30", T), T)
        self.assertEqual(first.decision, Decision.APPROVE)
        self.assertFalse(first.reused)

        # 多次批量重试：返回同一决定，不产生新退款、不重复计入累计
        for i in range(3):
            retry = engine.evaluate(
                req("R1", "O1", "C1", "30", T + timedelta(hours=1)),
                T + timedelta(hours=1),
            )
            self.assertTrue(retry.reused)
            self.assertEqual(retry.decision, Decision.APPROVE)
            self.assertEqual(retry.decision_at, T)

        record = engine.get_refund("R1")
        self.assertEqual(record.status, RefundStatus.EXECUTED)
        self.assertEqual(
            engine.executed_cumulative_for("C1", T + timedelta(hours=2)),
            Decimal("30"),
        )

    def test_inconsistent_retry_is_rejected(self):
        engine = make_engine(limit="100")
        engine.evaluate(req("R1", "O1", "C1", "30", T), T)

        # 相同业务编号但金额 / 订单 / 凭据变化，必须拒绝合并
        with self.assertRaises(ValueError):
            engine.evaluate(req("R1", "O1", "C1", "50", T), T)
        with self.assertRaises(ValueError):
            engine.evaluate(req("R1", "O2", "C1", "30", T), T)
        with self.assertRaises(ValueError):
            engine.evaluate(req("R1", "O1", "C9", "30", T), T)
        # 被拒绝的重试没有改变任何状态
        self.assertEqual(
            engine.executed_cumulative_for("C1", T), Decimal("30")
        )


class CumulativeThresholdTest(unittest.TestCase):
    """累计实际退款阈值：可接受结果、被阻止结果与等值边界。"""

    def test_boundary_equality_allowed_and_exceed_blocked(self):
        engine = make_engine(limit="100")

        d1 = engine.evaluate(req("R1", "O1", "C1", "60", T), T)
        self.assertEqual(d1.decision, Decision.APPROVE)

        # 60 + 40 == 100，等值边界必须放行
        d2 = engine.evaluate(
            req("R2", "O1", "C1", "40", T + timedelta(minutes=1)),
            T + timedelta(minutes=1),
        )
        self.assertEqual(d2.decision, Decision.APPROVE)
        self.assertEqual(d2.cumulative_refund, Decimal("60"))

        # 再超一分即挂起
        d3 = engine.evaluate(
            req("R3", "O1", "C1", "0.01", T + timedelta(minutes=2)),
            T + timedelta(minutes=2),
        )
        self.assertEqual(d3.decision, Decision.SUSPEND)
        self.assertIn(
            Reason.CUMULATIVE_THRESHOLD_EXCEEDED.value, d3.reasons
        )
        self.assertEqual(engine.get_refund("R3").status, RefundStatus.PENDING)

        # 挂起待决不计入累计：后续判定看到的累计仍是 100
        d4 = engine.evaluate(
            req("R4", "O1", "C1", "0.01", T + timedelta(minutes=3)),
            T + timedelta(minutes=3),
        )
        self.assertEqual(d4.decision, Decision.SUSPEND)
        self.assertEqual(d4.cumulative_refund, Decimal("100"))

    def test_manual_review_approve_counts_as_executed(self):
        engine = make_engine(limit="100")
        engine.evaluate(req("R1", "O1", "C1", "80", T), T)
        blocked = engine.evaluate(
            req("R2", "O1", "C1", "30", T + timedelta(minutes=1)),
            T + timedelta(minutes=1),
        )
        self.assertEqual(blocked.decision, Decision.SUSPEND)

        record = engine.review_pending_refund(
            "R2", approve=True, now=T + timedelta(hours=1)
        )
        self.assertEqual(record.status, RefundStatus.EXECUTED)
        self.assertEqual(
            engine.executed_cumulative_for("C1", T + timedelta(hours=1)),
            Decimal("110"),
        )

        # 复核拒绝则永远不计入累计
        blocked2 = engine.evaluate(
            req("R3", "O1", "C1", "20", T + timedelta(hours=2)),
            T + timedelta(hours=2),
        )
        self.assertEqual(blocked2.decision, Decision.SUSPEND)
        engine.review_pending_refund(
            "R3", approve=False, now=T + timedelta(hours=3)
        )
        self.assertEqual(
            engine.executed_cumulative_for("C1", T + timedelta(hours=4)),
            Decimal("110"),
        )


class SharedCredentialTest(unittest.TestCase):
    """不同订单共享收款凭据时，不得各自通过累计阈值。"""

    def test_orders_share_credential_cumulative(self):
        engine = make_engine(limit="100")

        d1 = engine.evaluate(req("R1", "ORDER-A", "C1", "70", T), T)
        self.assertEqual(d1.decision, Decision.APPROVE)

        # 另一订单复用同一收款凭据：70 + 40 > 100，必须挂起
        d2 = engine.evaluate(
            req("R2", "ORDER-B", "C1", "40", T + timedelta(minutes=1)),
            T + timedelta(minutes=1),
        )
        self.assertEqual(d2.decision, Decision.SUSPEND)
        self.assertEqual(d2.cumulative_refund, Decimal("70"))
        self.assertEqual(engine.get_refund("R2").status, RefundStatus.PENDING)

        # 待决的 R2 不占额度：ORDER-A 再退 30（70+30==100）可放行
        d3 = engine.evaluate(
            req("R3", "ORDER-A", "C1", "30", T + timedelta(minutes=2)),
            T + timedelta(minutes=2),
        )
        self.assertEqual(d3.decision, Decision.APPROVE)


class ChargebackConditionTest(unittest.TestCase):
    """已确认拒付条件的匹配范围与可配置阈值。"""

    def test_confirmed_chargeback_suspends_same_order_and_credential(self):
        engine = make_engine(limit="200", chargeback_limit=1)
        engine.evaluate(req("R1", "O1", "C1", "50", T), T)

        impact = engine.register_chargeback(
            Chargeback("CB1", "O1", "C1", confirmed_at=T + timedelta(days=1)),
            now=T + timedelta(days=1),
        )
        # R1 已执行：保留原决定并生成复核信号
        self.assertEqual(impact.re_decided, ())
        self.assertEqual(len(impact.review_signals), 1)
        self.assertEqual(impact.review_signals[0].refund_no, "R1")
        self.assertEqual(
            impact.review_signals[0].reason,
            Reason.LATE_CHARGEBACK_REVIEW.value,
        )
        self.assertEqual(engine.get_refund("R1").decision.decision,
                         Decision.APPROVE)

        # 同订单同凭据的新退款：阈值未超（100<=200），仅因拒付挂起
        d2 = engine.evaluate(
            req("R2", "O1", "C1", "50", T + timedelta(days=1, minutes=1)),
            T + timedelta(days=1, minutes=1),
        )
        self.assertEqual(d2.decision, Decision.SUSPEND)
        self.assertEqual(d2.reasons, (Reason.CONFIRMED_CHARGEBACK.value,))

        # 不同订单即使凭据相同也不受该拒付影响
        d3 = engine.evaluate(
            req("R3", "O2", "C1", "50", T + timedelta(days=1, minutes=2)),
            T + timedelta(days=1, minutes=2),
        )
        self.assertEqual(d3.decision, Decision.APPROVE)

        # 重复登记拒付必须报错
        with self.assertRaises(ValueError):
            engine.register_chargeback(
                Chargeback("CB1", "O1", "C1",
                           confirmed_at=T + timedelta(days=1)),
                now=T + timedelta(days=1, minutes=3),
            )

    def test_configurable_chargeback_limit(self):
        engine = make_engine(limit="1000", chargeback_limit=2)
        engine.register_credential(Credential("C1"))

        engine.register_chargeback(
            Chargeback("CB1", "O1", "C1", confirmed_at=T), now=T
        )
        d1 = engine.evaluate(req("R1", "O1", "C1", "10", T), T)
        self.assertEqual(d1.decision, Decision.APPROVE)  # 仅 1 笔，未达阈值

        engine.register_chargeback(
            Chargeback("CB2", "O1", "C1",
                       confirmed_at=T + timedelta(days=1)),
            now=T + timedelta(days=1),
        )
        d2 = engine.evaluate(
            req("R2", "O1", "C1", "10", T + timedelta(days=1)),
            T + timedelta(days=1),
        )
        self.assertEqual(d2.decision, Decision.SUSPEND)
        self.assertIn(Reason.CONFIRMED_CHARGEBACK.value, d2.reasons)


class LateChargebackRecomputeTest(unittest.TestCase):
    """拒付迟到：只重算相关待决退款，已执行 / 不相关者不动。"""

    def test_only_related_pending_refunds_recomputed(self):
        engine = make_engine(limit="150", chargeback_limit=1)

        # 凭据 C1 / 订单 O1：一笔已执行 + 一笔因阈值待决
        engine.evaluate(req("R1", "O1", "C1", "100", T), T)
        r2 = engine.evaluate(
            req("R2", "O1", "C1", "100", T + timedelta(hours=1)),
            T + timedelta(hours=1),
        )
        self.assertEqual(r2.decision, Decision.SUSPEND)
        r2_decided_at = engine.get_refund("R2").decided_at

        # 另一组凭据 / 订单上也有一笔待决退款，与本拒付不相关
        engine.evaluate(req("RX", "O9", "C9", "100", T), T)
        r_other = engine.evaluate(
            req("RY", "O9", "C9", "100", T + timedelta(hours=1)),
            T + timedelta(hours=1),
        )
        self.assertEqual(r_other.decision, Decision.SUSPEND)
        other_decided_at = engine.get_refund("RY").decided_at

        # 拒付迟到
        impact = engine.register_chargeback(
            Chargeback("CB1", "O1", "C1",
                       confirmed_at=T + timedelta(days=2)),
            now=T + timedelta(days=2),
        )

        # 已执行的 R1 维持原决定，仅产生复核信号
        self.assertEqual(engine.get_refund("R1").status, RefundStatus.EXECUTED)
        self.assertEqual(
            engine.get_refund("R1").decision.decision, Decision.APPROVE
        )
        self.assertEqual(len(impact.review_signals), 1)
        self.assertEqual(impact.review_signals[0].refund_no, "R1")

        # 仅相关的待决 R2 被重算，且现在同时命中阈值与拒付
        self.assertEqual(len(impact.re_decided), 1)
        self.assertEqual(impact.re_decided[0].refund_no, "R2")
        self.assertEqual(impact.re_decided[0].decision, Decision.SUSPEND)
        self.assertEqual(
            set(impact.re_decided[0].reasons),
            {
                Reason.CUMULATIVE_THRESHOLD_EXCEEDED.value,
                Reason.CONFIRMED_CHARGEBACK.value,
            },
        )
        self.assertGreater(
            engine.get_refund("R2").decided_at, r2_decided_at
        )

        # 不相关待决退款完全不动
        self.assertEqual(engine.get_refund("RY").decided_at, other_decided_at)
        self.assertEqual(
            engine.get_refund("RY").decision.decision, Decision.SUSPEND
        )
        self.assertEqual(len(engine.list_review_signals()), 1)


class CredentialHandoverTest(unittest.TestCase):
    """凭据确认替换后，旧凭据风险记录只在交接期内关联。"""

    def test_risk_records_linked_within_handover_window(self):
        engine = make_engine(limit="100", window=timedelta(days=7))
        engine.evaluate(req("R1", "O1", "C_OLD", "60", T), T)

        # 商户确认替换：C_NEW 接管 C_OLD
        engine.confirm_credential_replacement(
            "C_NEW", "C_OLD", confirmed_at=T + timedelta(days=1)
        )

        # 交接期内，新凭据承担旧凭据累计：60 + 50 > 100，被阻止
        d2 = engine.evaluate(
            req("R2", "O2", "C_NEW", "50", T + timedelta(days=2)),
            T + timedelta(days=2),
        )
        self.assertEqual(d2.decision, Decision.SUSPEND)
        self.assertEqual(d2.cumulative_refund, Decimal("60"))

        # 交接期内，旧凭据上的拒付同样关联到新凭据的同订单退款
        engine.register_chargeback(
            Chargeback("CB1", "O3", "C_OLD",
                       confirmed_at=T + timedelta(days=3)),
            now=T + timedelta(days=3),
        )
        d3 = engine.evaluate(
            req("R3", "O3", "C_NEW", "10", T + timedelta(days=3, minutes=1)),
            T + timedelta(days=3, minutes=1),
        )
        self.assertEqual(d3.decision, Decision.SUSPEND)
        self.assertIn(Reason.CONFIRMED_CHARGEBACK.value, d3.reasons)

    def test_risk_records_delinked_after_handover_window(self):
        engine = make_engine(limit="100", window=timedelta(days=7))
        engine.evaluate(req("R1", "O1", "C_OLD", "60", T), T)
        engine.confirm_credential_replacement(
            "C_NEW", "C_OLD", confirmed_at=T + timedelta(days=1)
        )

        # 交接期外：旧凭据累计不再关联，新凭据从 0 起算 -> 放行
        d2 = engine.evaluate(
            req("R2", "O2", "C_NEW", "90", T + timedelta(days=9)),
            T + timedelta(days=9),
        )
        self.assertEqual(d2.decision, Decision.APPROVE)
        self.assertEqual(d2.cumulative_refund, Decimal("0"))

        # 交接期外登记在旧凭据上的拒付也不再关联
        engine.register_chargeback(
            Chargeback("CB1", "O3", "C_OLD",
                       confirmed_at=T + timedelta(days=9)),
            now=T + timedelta(days=9),
        )
        d3 = engine.evaluate(
            req("R3", "O3", "C_NEW", "10", T + timedelta(days=9, minutes=1)),
            T + timedelta(days=9, minutes=1),
        )
        self.assertEqual(d3.decision, Decision.APPROVE)

    def test_handover_window_exact_boundary(self):
        engine = make_engine(limit="100", window=timedelta(days=7))
        engine.evaluate(req("R1", "O1", "C_OLD", "60", T), T)
        confirmed_at = T + timedelta(days=1)
        engine.confirm_credential_replacement(
            "C_NEW", "C_OLD", confirmed_at=confirmed_at
        )

        # 恰好第 7 天（== 交接期）仍关联：60 + 50 > 100 挂起
        d_at_boundary = engine.evaluate(
            req("R2", "O2", "C_NEW", "50",
                confirmed_at + timedelta(days=7)),
            confirmed_at + timedelta(days=7),
        )
        self.assertEqual(d_at_boundary.decision, Decision.SUSPEND)

        # 越过边界 1 微秒即脱钩：新凭据无累计，放行
        d_after = engine.evaluate(
            req("R3", "O3", "C_NEW", "50",
                confirmed_at + timedelta(days=7) + timedelta(microseconds=1)),
            confirmed_at + timedelta(days=7) + timedelta(microseconds=1),
        )
        self.assertEqual(d_after.decision, Decision.APPROVE)
        self.assertEqual(d_after.cumulative_refund, Decimal("0"))

    def test_unconfirmed_replacement_never_links(self):
        engine = make_engine(limit="100", window=timedelta(days=7))
        engine.evaluate(req("R1", "O1", "C_OLD", "60", T), T)
        # 不调用 confirm_credential_replacement：两凭据互不关联
        d2 = engine.evaluate(
            req("R2", "O2", "C_NEW", "90", T + timedelta(days=2)),
            T + timedelta(days=2),
        )
        self.assertEqual(d2.decision, Decision.APPROVE)
        self.assertEqual(d2.cumulative_refund, Decimal("0"))

    def test_replacement_chain_and_cycle_guard(self):
        engine = make_engine(limit="1000", window=timedelta(days=30))
        engine.confirm_credential_replacement(
            "C2", "C1", confirmed_at=T
        )
        engine.confirm_credential_replacement(
            "C3", "C2", confirmed_at=T + timedelta(days=1)
        )
        engine.evaluate(req("R1", "O1", "C1", "100", T), T)

        # 链上全部凭据在交接期内共享累计
        d = engine.evaluate(
            req("R2", "O2", "C3", "50", T + timedelta(days=2)),
            T + timedelta(days=2),
        )
        self.assertEqual(d.cumulative_refund, Decimal("100"))

        # 成环替换必须被阻止
        with self.assertRaises(ValueError):
            engine.confirm_credential_replacement(
                "C1", "C3", confirmed_at=T + timedelta(days=3)
            )


class InterleavingScenarioTest(unittest.TestCase):
    """重试、替换、拒付迟到、阈值与人工复核的交错场景。"""

    def test_full_interleaving_sequence(self):
        engine = make_engine(limit="200", window=timedelta(days=7))

        # 1) 首单放行，随后批量重试被合并
        d1 = engine.evaluate(req("R1", "O1", "C1", "120", T), T)
        self.assertEqual(d1.decision, Decision.APPROVE)
        retry = engine.evaluate(
            req("R1", "O1", "C1", "120", T + timedelta(hours=1)),
            T + timedelta(hours=1),
        )
        self.assertTrue(retry.reused)
        self.assertEqual(
            engine.executed_cumulative_for("C1", T + timedelta(hours=1)),
            Decimal("120"),
        )

        # 2) 凭据替换确认
        engine.confirm_credential_replacement(
            "C2", "C1", confirmed_at=T + timedelta(days=1)
        )

        # 3) 交接期内新凭据退款，因继承累计 120 而挂起（120+100>200）
        d2 = engine.evaluate(
            req("R2", "O2", "C2", "100", T + timedelta(days=2)),
            T + timedelta(days=2),
        )
        self.assertEqual(d2.decision, Decision.SUSPEND)
        self.assertEqual(engine.get_refund("R2").status, RefundStatus.PENDING)

        # 4) 旧凭据 O1 拒付迟到：R1 已执行 -> 信号；R2 订单不同 -> 不重算
        impact = engine.register_chargeback(
            Chargeback("CB1", "O1", "C1",
                       confirmed_at=T + timedelta(days=3)),
            now=T + timedelta(days=3),
        )
        self.assertEqual(len(impact.review_signals), 1)
        self.assertEqual(impact.re_decided, ())
        self.assertEqual(
            engine.get_refund("R1").decision.decision, Decision.APPROVE
        )

        # 5) 待决 R2 不占额度：120 + 80 == 200 等值边界放行
        d3 = engine.evaluate(
            req("R3", "O2", "C2", "80", T + timedelta(days=4)),
            T + timedelta(days=4),
        )
        self.assertEqual(d3.decision, Decision.APPROVE)

        # 6) 人工复核放行 R2，累计变为 300
        engine.review_pending_refund(
            "R2", approve=True, now=T + timedelta(days=5)
        )
        self.assertEqual(
            engine.executed_cumulative_for("C2", T + timedelta(days=5)),
            Decimal("300"),
        )

        # 7) 交接期外链路脱钩：C2 自身累计 180，180+50>200 仍挂起，
        #    但不再受 C1 的拒付 / 累计影响
        d4 = engine.evaluate(
            req("R4", "O3", "C2", "50", T + timedelta(days=10)),
            T + timedelta(days=10),
        )
        self.assertEqual(d4.decision, Decision.SUSPEND)
        self.assertEqual(
            d4.reasons, (Reason.CUMULATIVE_THRESHOLD_EXCEEDED.value,)
        )
        self.assertEqual(d4.cumulative_refund, Decimal("180"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
