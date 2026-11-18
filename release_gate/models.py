"""发布判定领域模型。

额度键为 ``(环境 ID, 控制项 ID)``：同一控制项即使被拆成多次豁免申请，
只要落在同一环境、同一考核窗口内，就共享同一条累计开放额度。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .intervals import Interval

OPEN = "OPEN"
CLOSE = "CLOSE"
_ACTIONS = (OPEN, CLOSE)


@dataclass(frozen=True)
class GateEvent:
    """环境网关的真实启停事件。"""

    event_id: str
    exemption_id: str
    env_id: str
    control_id: str
    action: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        action = self.action.upper()
        if action not in _ACTIONS:
            raise ValueError(f"未知事件动作 {self.action!r}，仅支持 OPEN/CLOSE")
        object.__setattr__(self, "action", action)


@dataclass(frozen=True)
class Exemption:
    """一条临时豁免的生效区间（实际开放区间还要结合启停事件判定）。"""

    exemption_id: str
    env_id: str
    control_id: str
    effective_start: datetime
    effective_end: datetime


@dataclass(frozen=True)
class ControlQuota:
    """某环境下某控制项在考核窗口内的累计开放额度。"""

    env_id: str
    control_id: str
    max_open_duration: timedelta


@dataclass(frozen=True)
class NewApplication:
    """待判定的新申请及其预计开放区间。"""

    application_id: str
    env_id: str
    control_id: str
    projected_intervals: tuple[Interval, ...] = field(default_factory=tuple)


@dataclass
class EvaluationBundle:
    """一次判定所需的完整输入。"""

    window: Interval
    environments: list[str]
    quotas: list[ControlQuota]
    exemptions: list[Exemption]
    events: list[GateEvent]
    applications: list[NewApplication]
    as_of: datetime | None = None
