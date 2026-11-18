"""退款风险判定组件的领域模型。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Optional, Tuple


def to_decimal(value: Any) -> Decimal:
    """把金额统一规范为 Decimal，避免 float 带来的精度问题。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class Decision(str, enum.Enum):
    """风险判定结果。"""

    APPROVE = "APPROVE"   # 自动放行（计入实际退款）
    SUSPEND = "SUSPEND"   # 挂起人工复核（不计入实际退款）


class RefundStatus(str, enum.Enum):
    """退款记录的处理状态。"""

    PENDING = "PENDING"     # 挂起待决
    EXECUTED = "EXECUTED"   # 已执行（自动放行或人工复核放行）
    REJECTED = "REJECTED"   # 人工复核拒绝


class Reason(str, enum.Enum):
    """判定 / 复核信号原因码。"""

    CUMULATIVE_THRESHOLD_EXCEEDED = "CUMULATIVE_THRESHOLD_EXCEEDED"
    CONFIRMED_CHARGEBACK = "CONFIRMED_CHARGEBACK"
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    LATE_CHARGEBACK_REVIEW = "LATE_CHARGEBACK_REVIEW"
    INCONSISTENT_RETRY = "INCONSISTENT_RETRY"


@dataclass(frozen=True)
class RefundRequest:
    """一笔退款请求（重试时携带相同的 refund_no）。"""

    refund_no: str
    order_id: str
    credential_id: str
    amount: Decimal
    requested_at: Any  # datetime，时区感知

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", to_decimal(self.amount))


@dataclass(frozen=True)
class Chargeback:
    """一笔已确认的拒付事件。"""

    chargeback_id: str
    order_id: str
    credential_id: str
    confirmed_at: Any  # datetime，拒付确认时间（可能晚于相关退款）


@dataclass(frozen=True)
class Credential:
    """收款凭据。"""

    credential_id: str
    merchant_id: str = ""
    active: bool = True


@dataclass(frozen=True)
class RiskConfig:
    """风险配置。

    cumulative_refund_limit: 同一收款凭据风险关联范围内允许的累计实际退款上限。
    chargeback_limit: 触发挂起的已确认拒付笔数阈值（默认 1 笔即挂起）。
    handover_window: 凭据替换后，旧凭据风险记录仍可关联的交接期。
    """

    cumulative_refund_limit: Any
    chargeback_limit: int = 1
    handover_window: timedelta = timedelta(days=30)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "cumulative_refund_limit", to_decimal(self.cumulative_refund_limit)
        )
        if self.chargeback_limit < 1:
            raise ValueError("chargeback_limit 必须 >= 1")
        if self.handover_window <= timedelta(0):
            raise ValueError("handover_window 必须为正的时间间隔")


@dataclass(frozen=True)
class RefundDecision:
    """一次风险判定的不可变结论。"""

    refund_no: str
    decision: Decision
    reasons: Tuple[str, ...]
    cumulative_refund: Decimal  # 判定时已执行的累计实际退款（不含本笔）
    decision_at: Any
    reused: bool = False  # True 表示来自合并的重试，未重新判定


@dataclass
class RefundRecord:
    """退款业务编号对应的唯一记录（重试合并到同一记录）。"""

    request: RefundRequest
    status: RefundStatus
    decision: RefundDecision
    decided_at: Any
    executed_at: Optional[Any] = None


@dataclass(frozen=True)
class ReviewSignal:
    """需要人工复核的信号。"""

    signal_id: str
    refund_no: Optional[str]
    reason: str
    created_at: Any
    detail: str = ""


@dataclass(frozen=True)
class ChargebackImpact:
    """迟到拒付登记后的影响面。"""

    chargeback_id: str
    re_decided: Tuple[RefundDecision, ...]
    review_signals: Tuple[ReviewSignal, ...]
