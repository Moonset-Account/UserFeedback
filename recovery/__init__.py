"""账户恢复会话模块。

公开入口：

* :class:`~recovery.session.RecoveryManager` —— 创建/查询恢复会话；
* :class:`~recovery.session.RecoverySession` —— 单个会话的状态机；
* :class:`~recovery.session.ManualClock` —— 测试用可控时钟；
* :class:`~recovery.factors.FactorRegistry` / :class:`~recovery.factors.Factor`
  —— 第二因素登记簿；
* :mod:`recovery.errors` 下的异常类型与 ``REASON_*`` 拒绝原因常量。
"""

from __future__ import annotations

from .errors import (
    RecoveryError,
    RecoveryRejected,
    RecoveryStateError,
    UnknownSessionError,
)
from .factors import Factor, FactorRegistry
from .session import (
    REASON_CODE_ALREADY_USED,
    REASON_CODE_MISMATCH,
    REASON_COOLDOWN,
    REASON_EXPIRED,
    REASON_FACTOR_NOT_FOUND,
    REASON_FINAL_STATE,
    REASON_NOT_CONFIRMED,
    REASON_TARGET_MISMATCH,
    REASON_WITHDRAWN,
    Decision,
    Event,
    EventType,
    ManualClock,
    RecoveryManager,
    RecoverySession,
    SessionStatus,
)

__all__ = [
    "RecoveryManager",
    "RecoverySession",
    "ManualClock",
    "SessionStatus",
    "EventType",
    "Event",
    "Decision",
    "Factor",
    "FactorRegistry",
    "RecoveryError",
    "RecoveryRejected",
    "RecoveryStateError",
    "UnknownSessionError",
    "REASON_WITHDRAWN",
    "REASON_NOT_CONFIRMED",
    "REASON_COOLDOWN",
    "REASON_EXPIRED",
    "REASON_CODE_MISMATCH",
    "REASON_CODE_ALREADY_USED",
    "REASON_TARGET_MISMATCH",
    "REASON_FACTOR_NOT_FOUND",
    "REASON_FINAL_STATE",
]
