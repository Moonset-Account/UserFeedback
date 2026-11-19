"""断言消费服务：条件核验 + 一次性消费登记（线程安全）。

核验顺序（任一环节失败即整体拒绝，且不落任何消费标记）：

1. 消费状态检查——断言此前已成功消费则拒绝重复投递；
2. 受众限制——每个限制组内命中一个受众即可（组内 OR），所有组都要满足（组间 AND）；
3. 候选确认项——至少一整项同时满足接收端、请求编号、半开有效区间；
   不允许把两个确认项的字段凑成一次匹配；
4. 以上全部通过后，在同一把断言级锁内写入消费记录（check-and-set）。

因此“发往错误接收端的请求”在第 3 步失败，根本不会执行第 4 步，
不会占用断言唯一的消费名额。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from .errors import (
    AssertionAlreadyConsumedError,
    AudienceRestrictionError,
    SubjectConfirmationError,
)
from .models import (
    Assertion,
    ConsumptionRecord,
    LoginRequest,
    LoginResult,
    MatchedConfirmation,
    SubjectConfirmation,
)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AssertionConsumerService:
    """以断言 ID 为键登记消费状态的内存服务。

    每个断言持有独立的 :class:`threading.Lock`，核验与消费登记在锁内一次完成，
    保证并发投递同一断言时恰好一次成功，其余投递得到
    :class:`AssertionAlreadyConsumedError`。
    """

    def __init__(self, clock: Optional[Clock] = None) -> None:
        self._clock: Clock = clock or _utc_now
        # 注册表自身的锁只保护下面两个字典的结构变更；
        # 具体断言的核验/消费由 per-assertion 锁保护。
        self._registry_lock = threading.Lock()
        self._locks: Dict[str, threading.Lock] = {}
        self._consumed: Dict[str, ConsumptionRecord] = {}

    # ---- 内部辅助 -------------------------------------------------------

    def _lock_for(self, assertion_id: str) -> threading.Lock:
        with self._registry_lock:
            lock = self._locks.get(assertion_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[assertion_id] = lock
            return lock

    @staticmethod
    def _check_audiences(assertion: Assertion, request: LoginRequest) -> None:
        groups = assertion.audience_restrictions
        if not groups:
            raise AudienceRestrictionError(
                assertion.assertion_id,
                request.audience,
                reason="assertion carries no audience restriction groups",
            )
        for index, group in enumerate(groups):
            if not group.permits(request.audience):
                raise AudienceRestrictionError(
                    assertion.assertion_id,
                    request.audience,
                    failed_group_index=index,
                    reason=f"none of the group audiences {list(group.audiences)} match",
                )

    @staticmethod
    def _match_confirmation(
        assertion: Assertion, request: LoginRequest, now: datetime
    ) -> SubjectConfirmation:
        confirmations = assertion.subject_confirmations
        if not confirmations:
            raise SubjectConfirmationError(
                assertion.assertion_id,
                ["assertion carries no subject confirmation candidates"],
            )
        all_reasons: list[str] = []
        for confirmation in confirmations:
            ok, reasons = confirmation.evaluate(
                request.recipient, request.in_response_to, now
            )
            if ok:
                # 命中的是一整项：recipient、in_response_to、时间窗由同一项同时满足
                return confirmation
            all_reasons.extend(reasons)
        raise SubjectConfirmationError(assertion.assertion_id, all_reasons)

    # ---- 对外 API -------------------------------------------------------

    def consume(self, assertion: Assertion, request: LoginRequest) -> LoginResult:
        """核验并消费断言；成功返回 :class:`LoginResult`，失败抛出联邦错误。

        线程安全：对同一 ``assertion_id`` 的并发调用串行化，且只有第一份
        通过全部核验的请求能写入消费记录。
        """
        lock = self._lock_for(assertion.assertion_id)
        with lock:
            now = self._clock()

            existing = self._consumed.get(assertion.assertion_id)
            if existing is not None:
                raise AssertionAlreadyConsumedError(
                    assertion.assertion_id, existing.delivery_id
                )

            # 下面两步只做校验，不修改任何状态；任何异常都会在写记录前传播出去。
            self._check_audiences(assertion, request)
            matched = self._match_confirmation(assertion, request, now)

            record = ConsumptionRecord(
                assertion_id=assertion.assertion_id,
                delivery_id=request.delivery_id,
                recipient=request.recipient,
                in_response_to=request.in_response_to,
                audience=request.audience,
                matched_confirmation_id=matched.confirmation_id,
                consumed_at=now,
            )
            # check-and-set：锁内最后一步落标记，此后同断言的投递一律被拒。
            self._consumed[assertion.assertion_id] = record

        return LoginResult(
            assertion_id=assertion.assertion_id,
            delivery_id=request.delivery_id,
            audience=request.audience,
            matched_confirmation=MatchedConfirmation.of(matched),
            consumed_at=now,
            record=record,
        )

    def is_consumed(self, assertion_id: str) -> bool:
        """返回断言是否已被成功消费。"""
        with self._registry_lock:
            return assertion_id in self._consumed

    def consumption_record(
        self, assertion_id: str
    ) -> Optional[ConsumptionRecord]:
        """返回成功消费记录；未消费时为 ``None``。"""
        with self._registry_lock:
            return self._consumed.get(assertion_id)
