"""事件模型。

所有事件都有两个时刻：
- occurred_at  业务发生时刻（业务世界里真实发生的时间）
- recorded_at  入账时刻（被写入台账、对系统可见的时间）

复算时按 recorded_at 过滤可见事件，业务结构（渠道版本、许可区间）
按 occurred_at 推导。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Event:
    """事件基类。"""

    event_id: str
    occurred_at: datetime  # 业务发生时刻
    recorded_at: datetime  # 入账时刻


@dataclass(frozen=True)
class ChannelRebind(Event):
    """渠道改绑。previous_value 为 None 表示初始绑定。

    每次改绑在其 occurred_at 产生一个新的渠道版本，
    版本有效期为 [occurred_at, 下一次改绑的 occurred_at)。
    """

    channel_kind: str  # "email" 或 "device"
    new_value: str
    previous_value: Optional[str] = None


@dataclass(frozen=True)
class EmailConfirmation(Event):
    """已验真的邮件确认（题面前提：输入已验真；verified=False 的不计入）。"""

    channel_value: str  # 确认发送到的邮件地址
    verified: bool = True


@dataclass(frozen=True)
class DeviceConfirmation(Event):
    """设备确认。"""

    device_id: str = ""


@dataclass(frozen=True)
class ServiceCredential(Event):
    """客服凭据。valid_until 为业务时刻口径的有效期上限（含端点）。"""

    credential_id: str = ""
    issued_by: str = ""
    valid_until: Optional[datetime] = None


@dataclass(frozen=True)
class RecoveryRequest(Event):
    """恢复请求。occurred_at 即发起时刻。

    risk_level: "high"（高风险，需两个不同恢复来源的证明）
                "normal"（至少一条有效证明）
    """

    risk_level: str = "high"
    operation: str = ""


@dataclass(frozen=True)
class Cancellation(Event):
    """取消事件，针对某个恢复请求。"""

    target_request_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Execution(Event):
    """敏感操作实际执行（决定已执行）的记录。"""

    request_id: str = ""
