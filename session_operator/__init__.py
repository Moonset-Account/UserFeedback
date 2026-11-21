"""按设备聚合会话的增量算子。

快速示例::

    from session_operator import SessionOperator

    operator = SessionOperator()
    operator.insert("e1", "A", 0, 12)
    operator.insert("e2", "A", 18, 7)

    operator.insert("e3", "A", 9, 5)
    operator.retract("e3")
"""

from .core import SessionOperator
from .errors import (
    DuplicateEventId,
    RejectedOperation,
    SealedSessionViolation,
    SessionOperatorError,
    UnknownEventId,
    WatermarkRegression,
    WatermarkViolation,
)
from .model import DEFAULT_GAP_SECONDS, Change, ChangeKind, Event, SessionResult

__all__ = [
    "SessionOperator",
    "Event",
    "SessionResult",
    "Change",
    "ChangeKind",
    "DEFAULT_GAP_SECONDS",
    "SessionOperatorError",
    "RejectedOperation",
    "DuplicateEventId",
    "UnknownEventId",
    "WatermarkViolation",
    "SealedSessionViolation",
    "WatermarkRegression",
]
