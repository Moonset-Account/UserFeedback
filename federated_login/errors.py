"""联合登录核验阶段的错误类型。

所有错误都继承 :class:`FederationError`，便于调用方统一捕获。
注意：抛出任何核验错误都不会改变断言的消费状态——失败请求不占消费名额。
"""

from __future__ import annotations

from typing import Optional


class FederationError(Exception):
    """联合登录核验相关错误的基类。"""


class AssertionAlreadyConsumedError(FederationError):
    """断言此前已被成功消费，重复投递被拒绝。"""

    def __init__(self, assertion_id: str, consumed_by: Optional[str] = None) -> None:
        self.assertion_id = assertion_id
        self.consumed_by = consumed_by
        detail = f" by delivery {consumed_by!r}" if consumed_by else ""
        super().__init__(
            f"assertion {assertion_id!r} has already been consumed{detail}"
        )


class AudienceRestrictionError(FederationError):
    """受众限制核验失败：某个限制组内没有一个受众命中当前应用。"""

    def __init__(
        self,
        assertion_id: str,
        audience: str,
        failed_group_index: Optional[int] = None,
        reason: str = "",
    ) -> None:
        self.assertion_id = assertion_id
        self.audience = audience
        self.failed_group_index = failed_group_index
        self.reason = reason
        location = (
            f"audience restriction group #{failed_group_index}"
            if failed_group_index is not None
            else "audience restrictions"
        )
        super().__init__(
            f"audience {audience!r} not permitted by {location} "
            f"of assertion {assertion_id!r}: {reason}"
        )


class SubjectConfirmationError(FederationError):
    """候选确认项核验失败：不存在任何一整项同时适配当前请求。"""

    def __init__(self, assertion_id: str, reasons: Optional[list[str]] = None) -> None:
        self.assertion_id = assertion_id
        self.reasons: tuple[str, ...] = tuple(reasons or ())
        joined = "; ".join(self.reasons) if self.reasons else "no usable confirmation"
        super().__init__(
            f"no subject confirmation of assertion {assertion_id!r} "
            f"matches the whole request: {joined}"
        )
