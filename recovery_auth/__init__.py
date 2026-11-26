"""恢复授权判定模块。

公开 API：
- 事件：ChannelRebind, EmailConfirmation, DeviceConfirmation,
  ServiceCredential, RecoveryRequest, Cancellation, Execution
- 台账：Ledger
- 引擎：AuthorizationEngine, EngineConfig
- 结果：Decision, EvidenceChain, EvidenceLink, RebindCrossing,
  ChannelVersion, Interval, Correction, DecisionSummary
"""

from .engine import (
    SOURCE_DEVICE,
    SOURCE_EMAIL,
    SOURCE_SERVICE,
    AuthorizationEngine,
    EngineConfig,
)
from .events import (
    Cancellation,
    ChannelRebind,
    DeviceConfirmation,
    EmailConfirmation,
    Event,
    Execution,
    RecoveryRequest,
    ServiceCredential,
)
from .ledger import Ledger
from .result import (
    ChannelVersion,
    Correction,
    Decision,
    DecisionSummary,
    EvidenceChain,
    EvidenceLink,
    Interval,
    RebindCrossing,
)

__all__ = [
    "AuthorizationEngine",
    "Cancellation",
    "ChannelRebind",
    "ChannelVersion",
    "Correction",
    "Decision",
    "DecisionSummary",
    "DeviceConfirmation",
    "EmailConfirmation",
    "EngineConfig",
    "Event",
    "EvidenceChain",
    "EvidenceLink",
    "Execution",
    "Interval",
    "Ledger",
    "RebindCrossing",
    "RecoveryRequest",
    "ServiceCredential",
    "SOURCE_DEVICE",
    "SOURCE_EMAIL",
    "SOURCE_SERVICE",
]
