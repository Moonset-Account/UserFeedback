"""领域模型：已验签断言、候选确认项、登录请求与消费结果。

时间约定
--------
所有时间字段都必须是**带时区**的 :class:`datetime.datetime`（``tzinfo`` 非空），
有效区间按半开区间 ``[not_before, not_on_or_after)`` 判定：
左边界时刻命中，右边界时刻失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional


def _require_aware(dt: datetime, field_name: str) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")


@dataclass(frozen=True)
class SubjectConfirmation:
    """断言中的一个候选 Bearer 确认项。

    三个条件（接收端、请求编号、有效区间）必须由**同一个确认项**同时满足，
    核验逻辑不允许把不同确认项的字段拼起来适配请求。
    """

    confirmation_id: str
    recipient: str
    in_response_to: str
    not_before: datetime
    not_on_or_after: datetime

    def __post_init__(self) -> None:
        if not self.confirmation_id:
            raise ValueError("confirmation_id must not be empty")
        if not self.recipient:
            raise ValueError("recipient must not be empty")
        if not self.in_response_to:
            raise ValueError("in_response_to must not be empty")
        _require_aware(self.not_before, "not_before")
        _require_aware(self.not_on_or_after, "not_on_or_after")
        if self.not_on_or_after <= self.not_before:
            raise ValueError(
                "not_on_or_after must be strictly later than not_before"
            )

    def evaluate(
        self, recipient: str, in_response_to: str, now: datetime
    ) -> tuple[bool, tuple[str, ...]]:
        """返回 ``(是否整体适配, 不满足原因列表)``。

        三个条件独立评估并一次性返回全部失败原因，便于错误信息说明
        “为什么没有任何一整项适配”，而不是只报第一个不匹配的字段。
        """
        reasons: list[str] = []
        if recipient != self.recipient:
            reasons.append(
                f"{self.confirmation_id}: recipient mismatch "
                f"(request {recipient!r} != confirmation {self.recipient!r})"
            )
        if in_response_to != self.in_response_to:
            reasons.append(
                f"{self.confirmation_id}: in_response_to mismatch "
                f"(request {in_response_to!r} != confirmation "
                f"{self.in_response_to!r})"
            )
        # 半开区间 [not_before, not_on_or_after)
        if now < self.not_before:
            reasons.append(
                f"{self.confirmation_id}: not yet valid at {now.isoformat()} "
                f"(not_before={self.not_before.isoformat()})"
            )
        elif now >= self.not_on_or_after:
            reasons.append(
                f"{self.confirmation_id}: expired at {now.isoformat()} "
                f"(not_on_or_after={self.not_on_or_after.isoformat()})"
            )
        return (not reasons, tuple(reasons))


@dataclass(frozen=True)
class AudienceRestriction:
    """一个受众限制组。

    组内为“或”关系：当前应用命中组内任意一个受众即满足本组。
    不同组之间为“与”关系：断言携带的所有组都必须满足，由 service 层串联。
    """

    audiences: tuple[str, ...]

    def __post_init__(self) -> None:
        # dataclass frozen 时仍可通过 object.__setattr__ 规整入参
        normalized = tuple(a for a in self.audiences if a)
        if not normalized:
            raise ValueError("audience restriction must contain at least one audience")
        object.__setattr__(self, "audiences", normalized)

    def permits(self, audience: str) -> bool:
        return audience in self.audiences


@dataclass(frozen=True)
class Assertion:
    """一份已经通过签名验证的断言对象。

    本模型假定上游已完成 XML 解析与验签；此处只承载条件核验所需信息。
    受众限制与候选确认项为空都属于“无法适配任何请求”，在 service 层被拒绝。
    """

    assertion_id: str
    audience_restrictions: tuple[AudienceRestriction, ...]
    subject_confirmations: tuple[SubjectConfirmation, ...]

    def __post_init__(self) -> None:
        if not self.assertion_id:
            raise ValueError("assertion_id must not be empty")
        object.__setattr__(
            self, "audience_restrictions", tuple(self.audience_restrictions)
        )
        object.__setattr__(
            self, "subject_confirmations", tuple(self.subject_confirmations)
        )


@dataclass(frozen=True)
class LoginRequest:
    """当前到达联合登录入口的一次登录请求。"""

    audience: str
    recipient: str
    in_response_to: str
    delivery_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.audience:
            raise ValueError("audience must not be empty")
        if not self.recipient:
            raise ValueError("recipient must not be empty")
        if not self.in_response_to:
            raise ValueError("in_response_to must not be empty")


@dataclass(frozen=True)
class MatchedConfirmation:
    """成功命中的候选确认项快照。"""

    confirmation_id: str
    recipient: str
    in_response_to: str
    not_before: datetime
    not_on_or_after: datetime

    @classmethod
    def of(cls, confirmation: SubjectConfirmation) -> "MatchedConfirmation":
        return cls(
            confirmation_id=confirmation.confirmation_id,
            recipient=confirmation.recipient,
            in_response_to=confirmation.in_response_to,
            not_before=confirmation.not_before,
            not_on_or_after=confirmation.not_on_or_after,
        )


@dataclass(frozen=True)
class ConsumptionRecord:
    """一次成功消费的不可变记录，用于事后辨明并发场景下哪次投递成功。"""

    assertion_id: str
    delivery_id: Optional[str]
    recipient: str
    in_response_to: str
    audience: str
    matched_confirmation_id: str
    consumed_at: datetime


@dataclass(frozen=True)
class LoginResult:
    """登录成功结果。拿到该对象即代表断言已完成一次性消费。"""

    assertion_id: str
    delivery_id: Optional[str]
    audience: str
    matched_confirmation: MatchedConfirmation
    consumed_at: datetime
    record: ConsumptionRecord


def freeze_restrictions(
    groups: Iterable[Iterable[str]],
) -> tuple[AudienceRestriction, ...]:
    """便捷构造：把字符串列表的列表转成受众限制组元组。"""
    return tuple(AudienceRestriction(tuple(group)) for group in groups)
