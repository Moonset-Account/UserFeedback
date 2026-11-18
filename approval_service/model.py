"""领域模型：目录记录、请求、策略与决定（全部为不可变值对象）。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple


class Outcome(str, enum.Enum):
    """授权决定的三种结果。"""

    ALLOW = "allow"
    DENY = "deny"
    MANUAL_REVIEW = "manual_review"


class ReviewOutcome(str, enum.Enum):
    """复核结论。"""

    UPHELD = "upheld"            # 维持原决定
    OVERTURNED = "overturned"    # 历史更正改写了原结论
    REINFORCED = "reinforced"    # 原转人工决定得到明确的复核结论


def require_aware(dt: datetime, name: str) -> datetime:
    """所有时间点必须带时区，避免朴素时间造成的生效/接收边界歧义。"""
    if not isinstance(dt, datetime):
        raise TypeError(f"{name} 必须是 datetime 实例")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{name} 必须是带时区的 datetime")
    return dt


@dataclass(frozen=True)
class DirectoryRecord:
    """一条目录修订记录。

    effective_at 与 received_at 分别建模“生效时间”和“系统接收时间”：
    迟到更新与历史更正的 received_at 可以晚于 effective_at。
    supersedes 显式声明该记录更正/取代的既往记录 id。
    """

    record_id: str
    subject_id: str
    department: str
    on_duty: bool
    effective_at: datetime
    received_at: datetime
    supersedes: Optional[str] = None
    source: str = "directory"

    def __post_init__(self) -> None:
        require_aware(self.effective_at, "effective_at")
        require_aware(self.received_at, "received_at")
        if not self.record_id:
            raise ValueError("record_id 不能为空")
        if not self.subject_id:
            raise ValueError("subject_id 不能为空")


@dataclass(frozen=True)
class AccessRequest:
    """一次资源访问请求。

    普通请求需要 business_line 以匹配业务允许范围；敏感资源的 business_line
    可为 None。
    """

    request_id: str
    subject_id: str
    resource_id: str
    business_line: Optional[str]
    requested_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.requested_at, "requested_at")
        if not self.request_id:
            raise ValueError("request_id 不能为空")


@dataclass(frozen=True)
class ResourceRule:
    """资源规则。

    sensitive=True 时走敏感资源判定；allowed_departments 为 None 表示
    不限部门，否则主体部门必须在允许集合内。
    """

    resource_id: str
    sensitive: bool
    allowed_departments: Optional[frozenset] = None


@dataclass(frozen=True)
class BusinessScope:
    """普通请求所属业务的允许范围。"""

    business_line: str
    allowed_departments: frozenset
    require_on_duty: bool = True


@dataclass(frozen=True)
class Decision:
    """系统依据“判定时刻可见输入”作出的实际决定，永久保存、不可变。"""

    request_id: str
    subject_id: str
    resource_id: str
    business_line: Optional[str]
    sensitive: bool
    outcome: Outcome
    reasons: Tuple[str, ...]
    decided_at: datetime
    applicable_record_id: Optional[str]
    visible_record_ids: Tuple[str, ...]
    visible_inputs: Tuple[DirectoryRecord, ...]
    input_fingerprint: str


@dataclass(frozen=True)
class ReviewReport:
    """复核报告。复核只产生新报告，绝不改写原始 Decision。"""

    request_id: str
    reviewed_at: datetime
    original_outcome: Outcome
    current_outcome: Outcome
    review_outcome: ReviewOutcome
    visible_inputs_changed: bool
    applicable_data_changed: bool
    conflict: bool
    stale: bool
    reasons: Tuple[str, ...]
