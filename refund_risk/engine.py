"""退款风险判定引擎。

设计要点：

1. 合并重试：以退款业务编号 ``refund_no`` 为幂等键，重试只返回原决定，
   不会产生第二笔退款；重试载荷与首单的订单 / 凭据 / 金额不一致时直接拒绝。
2. 风险关联范围：同一凭据，以及在配置交接期内经商户确认替换形成的凭据链，
   共享累计实际退款与拒付计数；超过交接期后旧凭据风险记录不再关联。
3. 累计实际退款：只统计已执行（EXECUTED）退款，挂起待决（PENDING）不计入，
   防止多笔挂起请求各自"看到"同一额度而并发通过。
4. 拒付迟到：只重算相关的待决退款；已执行退款保留原决定，仅生成复核信号。
5. 凭据替换：必须经商户确认；确认时刻起的交接期内旧凭据风险记录继续关联，
   交接期外自动脱钩。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import count
from typing import Dict, List, Optional, Set, Tuple

from .models import (
    Chargeback,
    ChargebackImpact,
    Credential,
    Decision,
    Reason,
    RefundDecision,
    RefundRecord,
    RefundRequest,
    RefundStatus,
    ReviewSignal,
    RiskConfig,
)


class RefundRiskEngine:
    """有状态的退款风险判定组件（单实例内维护全部状态，非线程安全）。"""

    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        # 凭据台账
        self._credentials: Dict[str, Credential] = {}
        # 经商户确认的凭据替换边：新凭据 -> (旧凭据, 确认时间)
        self._replacements: Dict[str, Tuple[str, datetime]] = {}
        # 退款业务编号 -> 唯一记录（重试合并）
        self._refunds: Dict[str, RefundRecord] = {}
        # 已确认拒付：chargeback_id -> Chargeback
        self._chargebacks: Dict[str, Chargeback] = {}
        # 复核信号
        self._signals: List[ReviewSignal] = []
        self._signal_seq = count(1)

    # ------------------------------------------------------------------
    # 凭据管理
    # ------------------------------------------------------------------
    def register_credential(self, credential: Credential) -> None:
        """登记收款凭据；重复登记以最新状态为准。"""
        self._credentials[credential.credential_id] = credential

    def confirm_credential_replacement(
        self,
        new_credential_id: str,
        old_credential_id: str,
        confirmed_at: datetime,
    ) -> None:
        """记录商户确认的凭据替换。

        交接期从确认时刻起算；同一新凭据重复确认替换会报错，避免覆盖原链路。
        """
        if new_credential_id == old_credential_id:
            raise ValueError("新旧凭据不能相同")
        if new_credential_id in self._replacements:
            raise ValueError(f"凭据 {new_credential_id} 已确认过替换")
        # 从旧凭据沿替换链向上追溯：新凭据若已是其祖先，再加边必然成环
        ancestor = old_credential_id
        seen: Set[str] = set()
        while ancestor in self._replacements and ancestor not in seen:
            seen.add(ancestor)
            prev, _confirmed = self._replacements[ancestor]
            if prev == new_credential_id:
                raise ValueError("凭据替换链不允许形成环")
            ancestor = prev
        self._replacements[new_credential_id] = (old_credential_id, confirmed_at)

    def _risk_lineage(self, credential_id: str, now: datetime) -> Set[str]:
        """返回 ``now`` 时刻某凭据可关联到的全部风险凭据。

        从当前凭据沿"新凭据 -> 旧凭据"的替换边向上回溯：每条边只有在
        其确认时刻尚未超出交接期时才可跨越，任一边脱钩即停止，更早的
        祖先凭据也不再关联。旧凭据自身的风险记录始终保留在结果中。
        """
        result: Set[str] = {credential_id}
        current = credential_id
        while current in self._replacements:
            prev, confirmed_at = self._replacements[current]
            if now - confirmed_at > self._config.handover_window:
                break  # 交接期外：旧凭据风险记录脱钩，链路到此为止
            result.add(prev)
            current = prev
        return result

    # ------------------------------------------------------------------
    # 风险事实查询
    # ------------------------------------------------------------------
    def _executed_cumulative(self, lineage: Set[str], now: datetime) -> Decimal:
        """风险关联范围内、截至 now 的已执行累计实际退款。"""
        total = Decimal("0")
        for record in self._refunds.values():
            if record.status != RefundStatus.EXECUTED:
                continue
            if record.request.credential_id not in lineage:
                continue
            if record.executed_at is not None and record.executed_at > now:
                continue
            total += record.request.amount
        return total

    def _confirmed_chargeback_count(
        self, order_id: str, lineage: Set[str], now: datetime
    ) -> int:
        """相关已确认拒付数：凭据在关联范围内即命中，订单号同时命中。"""
        count_ = 0
        for cb in self._chargebacks.values():
            if cb.confirmed_at > now:
                continue
            if cb.credential_id in lineage and cb.order_id == order_id:
                count_ += 1
        return count_

    # ------------------------------------------------------------------
    # 退款判定
    # ------------------------------------------------------------------
    def evaluate(self, request: RefundRequest, now: datetime) -> RefundDecision:
        """对一笔退款请求作出自动放行 / 挂起复核判定。

        相同 ``refund_no`` 的重试被合并，直接返回原决定且不改变任何状态。
        """
        existing = self._refunds.get(request.refund_no)
        if existing is not None:
            first = existing.request
            if (
                first.order_id != request.order_id
                or first.credential_id != request.credential_id
                or first.amount != request.amount
            ):
                raise ValueError(
                    f"退款 {request.refund_no} 的重试载荷与首单不一致，拒绝合并"
                )
            return RefundDecision(
                refund_no=existing.decision.refund_no,
                decision=existing.decision.decision,
                reasons=existing.decision.reasons,
                cumulative_refund=existing.decision.cumulative_refund,
                decision_at=existing.decision.decision_at,
                reused=True,
            )

        # 自动登记首次出现的凭据
        if request.credential_id not in self._credentials:
            self._credentials[request.credential_id] = Credential(
                credential_id=request.credential_id
            )

        lineage = self._risk_lineage(request.credential_id, now)
        cumulative = self._executed_cumulative(lineage, now)
        chargebacks = self._confirmed_chargeback_count(
            request.order_id, lineage, now
        )

        reasons: List[str] = []
        if cumulative + request.amount > self._config.cumulative_refund_limit:
            reasons.append(Reason.CUMULATIVE_THRESHOLD_EXCEEDED.value)
        if chargebacks >= self._config.chargeback_limit:
            reasons.append(Reason.CONFIRMED_CHARGEBACK.value)

        decision_enum = Decision.SUSPEND if reasons else Decision.APPROVE
        decision = RefundDecision(
            refund_no=request.refund_no,
            decision=decision_enum,
            reasons=tuple(reasons),
            cumulative_refund=cumulative,
            decision_at=now,
            reused=False,
        )

        if decision_enum is Decision.APPROVE:
            record = RefundRecord(
                request=request,
                status=RefundStatus.EXECUTED,
                decision=decision,
                decided_at=now,
                executed_at=now,
            )
        else:
            record = RefundRecord(
                request=request,
                status=RefundStatus.PENDING,
                decision=decision,
                decided_at=now,
            )
        self._refunds[request.refund_no] = record
        return decision

    # ------------------------------------------------------------------
    # 拒付事件（含迟到）
    # ------------------------------------------------------------------
    def register_chargeback(
        self, chargeback: Chargeback, now: datetime
    ) -> ChargebackImpact:
        """登记一笔已确认拒付，并处理其对现有退款的影响。

        - 待决（PENDING）退款且与拒付相关：基于当前事实重新判定；
        - 已执行（EXECUTED）且相关：保留原决定，生成迟到拒付复核信号；
        - 不相关的退款一律不动。
        """
        if chargeback.chargeback_id in self._chargebacks:
            raise ValueError(f"拒付 {chargeback.chargeback_id} 已登记")
        self._chargebacks[chargeback.chargeback_id] = chargeback
        if chargeback.credential_id not in self._credentials:
            self._credentials[chargeback.credential_id] = Credential(
                credential_id=chargeback.credential_id
            )

        re_decided: List[RefundDecision] = []
        signals: List[ReviewSignal] = []

        for record in list(self._refunds.values()):
            lineage = self._risk_lineage(record.request.credential_id, now)
            related = (
                chargeback.credential_id in lineage
                and chargeback.order_id == record.request.order_id
            )
            if not related:
                continue

            if record.status is RefundStatus.EXECUTED:
                # 已执行退款保留原决定，仅生成复核信号
                signal = self._add_signal(
                    refund_no=record.request.refund_no,
                    reason=Reason.LATE_CHARGEBACK_REVIEW,
                    now=now,
                    detail=(
                        f"拒付 {chargeback.chargeback_id} 迟到，"
                        f"已执行退款 {record.request.refund_no} 维持原决定 "
                        f"{record.decision.decision.value}，转人工复核"
                    ),
                )
                signals.append(signal)
                continue

            if record.status is not RefundStatus.PENDING:
                continue

            # 仅重算相关的待决退款
            new_decision = self._recompute(record, now)
            re_decided.append(new_decision)

        return ChargebackImpact(
            chargeback_id=chargeback.chargeback_id,
            re_decided=tuple(re_decided),
            review_signals=tuple(signals),
        )

    def _recompute(self, record: RefundRecord, now: datetime) -> RefundDecision:
        """基于当前事实重新判定一笔待决退款。"""
        request = record.request
        lineage = self._risk_lineage(request.credential_id, now)
        cumulative = self._executed_cumulative(lineage, now)
        chargebacks = self._confirmed_chargeback_count(
            request.order_id, lineage, now
        )

        reasons: List[str] = []
        if cumulative + request.amount > self._config.cumulative_refund_limit:
            reasons.append(Reason.CUMULATIVE_THRESHOLD_EXCEEDED.value)
        if chargebacks >= self._config.chargeback_limit:
            reasons.append(Reason.CONFIRMED_CHARGEBACK.value)

        decision_enum = Decision.SUSPEND if reasons else Decision.APPROVE
        decision = RefundDecision(
            refund_no=request.refund_no,
            decision=decision_enum,
            reasons=tuple(reasons),
            cumulative_refund=cumulative,
            decision_at=now,
            reused=False,
        )
        record.decision = decision
        record.decided_at = now
        if decision_enum is Decision.APPROVE:
            # 重算后满足放行条件：自动执行并计入累计实际退款
            record.status = RefundStatus.EXECUTED
            record.executed_at = now
        return decision

    # ------------------------------------------------------------------
    # 人工复核入口
    # ------------------------------------------------------------------
    def review_pending_refund(
        self, refund_no: str, approve: bool, now: datetime
    ) -> RefundRecord:
        """人工复核待决退款：放行则执行并计入累计，拒绝则置为 REJECTED。

        原始自动判定保留在记录历史中（record.decision 被复核结论替换前，
        调用方可先读取），状态变更可被后续累计与拒付逻辑感知。
        """
        record = self._refunds.get(refund_no)
        if record is None:
            raise KeyError(f"未知退款业务编号: {refund_no}")
        if record.status is not RefundStatus.PENDING:
            raise ValueError(
                f"退款 {refund_no} 当前状态 {record.status.value}，不可复核"
            )

        if approve:
            lineage = self._risk_lineage(record.request.credential_id, now)
            cumulative = self._executed_cumulative(lineage, now)
            decision = RefundDecision(
                refund_no=refund_no,
                decision=Decision.APPROVE,
                reasons=tuple(record.decision.reasons),
                cumulative_refund=cumulative,
                decision_at=now,
                reused=False,
            )
            record.decision = decision
            record.decided_at = now
            record.status = RefundStatus.EXECUTED
            record.executed_at = now
        else:
            record.status = RefundStatus.REJECTED
        return record

    # ------------------------------------------------------------------
    # 信号与只读视图
    # ------------------------------------------------------------------
    def _add_signal(
        self, refund_no: Optional[str], reason: Reason, now: datetime, detail: str
    ) -> ReviewSignal:
        signal = ReviewSignal(
            signal_id=f"SIG-{next(self._signal_seq):06d}",
            refund_no=refund_no,
            reason=reason.value,
            created_at=now,
            detail=detail,
        )
        self._signals.append(signal)
        return signal

    def get_refund(self, refund_no: str) -> RefundRecord:
        """按退款业务编号读取唯一记录（重试合并后的同一条）。"""
        if refund_no not in self._refunds:
            raise KeyError(f"未知退款业务编号: {refund_no}")
        return self._refunds[refund_no]

    def list_review_signals(self) -> Tuple[ReviewSignal, ...]:
        """返回全部复核信号（迟到拒付等）。"""
        return tuple(self._signals)

    def executed_cumulative_for(self, credential_id: str, now: datetime) -> Decimal:
        """对外暴露某凭据当前风险关联范围内的累计实际退款。"""
        lineage = self._risk_lineage(credential_id, now)
        return self._executed_cumulative(lineage, now)
