"""判定结果模型：许可有效区间、证据链、更正。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple


@dataclass(frozen=True)
class ChannelVersion:
    """渠道版本：一次改绑产生的绑定状态，业务时间区间 [start, end)。"""

    channel_kind: str
    version: int  # 从 1 开始的序号
    value: str
    start: datetime
    end: Optional[datetime]  # None 表示当前版本（开口区间）
    rebind_event_id: str

    def covers(self, t: datetime) -> bool:
        return self.start <= t and (self.end is None or t < self.end)


@dataclass(frozen=True)
class Interval:
    """许可有效区间 [start, end)。"""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class EvidenceLink:
    """证据链中的一条证明及其归属结论。"""

    proof_event_id: str
    source: str  # 恢复来源: "email" / "device" / "service"
    occurred_at: datetime
    channel_version: Optional[ChannelVersion]  # 客服凭据无渠道版本，为 None
    accepted: bool
    reason: str


@dataclass(frozen=True)
class RebindCrossing:
    """证据链跨越的一次改绑边界。"""

    rebind_event_id: str
    channel_kind: str
    from_version: Optional[int]  # 初始绑定时为 None
    to_version: int
    occurred_at: datetime


@dataclass(frozen=True)
class EvidenceChain:
    """证据链：证明归属 + 跨改绑边界的版本迁移。"""

    links: Tuple[EvidenceLink, ...]
    crossings: Tuple[RebindCrossing, ...]
    distinct_sources: frozenset


@dataclass(frozen=True)
class DecisionSummary:
    """一次判定的摘要，用于更正中对比如此/当时的结论。"""

    granted: bool
    revoked: bool
    validity_interval: Optional[Interval]
    distinct_sources: Tuple[str, ...]


@dataclass(frozen=True)
class Correction:
    """对已执行决定的更正：附当时可见的依据与当前复算结论。"""

    executed_at: datetime
    then_visible_basis: DecisionSummary  # 当时（执行时刻入账点）可见的依据
    current_finding: DecisionSummary  # 当前复算结论
    changed: bool
    reason: str


@dataclass(frozen=True)
class Decision:
    """对一次恢复请求的判定输出。"""

    request_id: str
    as_of: Optional[datetime]
    granted: bool  # 证明是否充分（不考虑取消）
    revoked: bool  # 是否被可见的取消推翻/截断
    validity_interval: Optional[Interval]  # 许可有效区间；拒绝或完全推翻时为 None
    evidence_chain: EvidenceChain
    correction: Optional[Correction]  # 存在已执行记录时附更正与当时依据

    @property
    def status(self) -> str:
        if not self.granted:
            return "denied"
        if self.revoked:
            return "revoked" if self.validity_interval is None else "truncated"
        return "permitted"
