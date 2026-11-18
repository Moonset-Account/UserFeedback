"""账户恢复会话状态机。

业务规则（题面）：

1. 恢复申请进入带时限的冷静期，冷静期结束前不能使用恢复码重新登记；
   整个会话还有一个最长有效期，过期后恢复码作废。
2. 会话过程记录四类事实：旧设备确认、用户撤回、恢复码使用、重新登记结果，
   全部以只追加事件的形式保存在 :attr:`RecoverySession.events` 里。
3. 撤回发生在重新登记之前时，立即作废恢复会话，原有因素继续可用。
4. 恢复成功只允许替换申请中指定的那个因素，不能借恢复流程改动其他因素。
5. 两次并发提交（无论请求编号是否相同）最多成功一次。
6. 客户端超时后用相同请求编号重试，必须取回上一次的原决定（成功或拒绝），
   不会因为重试而二次执行。
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from .errors import RecoveryStateError
from .factors import Factor, FactorRegistry


class SessionStatus(str, Enum):
    """恢复会话生命周期状态。"""

    PENDING = "pending"        # 冷静期进行中
    ACTIVE = "active"          # 冷静期结束，等待使用恢复码
    RECOVERED = "recovered"    # 重新登记成功（终态）
    WITHDRAWN = "withdrawn"    # 用户撤回（终态）
    EXPIRED = "expired"        # 超过会话有效期仍未完成（终态）


class EventType(str, Enum):
    """只追加事件流中可能出现的事件类型。"""

    CREATED = "created"
    OLD_DEVICE_CONFIRMED = "old_device_confirmed"
    WITHDRAWN = "withdrawn"
    RECOVERY_CODE_USED = "recovery_code_used"
    REENROLLMENT_REJECTED = "reenrollment_rejected"
    REENROLLED = "reenrolled"
    EXPIRED = "expired"


# 拒绝原因常量，调用方按 reason 分支处理，避免依赖错误文案。
REASON_WITHDRAWN = "withdrawn"
REASON_NOT_CONFIRMED = "old_device_not_confirmed"
REASON_COOLDOWN = "cooldown_active"
REASON_EXPIRED = "session_expired"
REASON_CODE_MISMATCH = "recovery_code_mismatch"
REASON_CODE_ALREADY_USED = "recovery_code_already_used"
REASON_TARGET_MISMATCH = "target_factor_mismatch"
REASON_FACTOR_NOT_FOUND = "target_factor_not_found"
REASON_FINAL_STATE = "session_in_final_state"


@dataclass(frozen=True)
class Event:
    """不可变事件记录。"""

    seq: int
    event_type: EventType
    timestamp: float
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    """一次提交的原决定，按请求编号缓存以支持幂等重试。"""

    request_id: str
    accepted: bool
    reason: Optional[str]
    message: str
    session_id: str
    new_factor_id: Optional[str]
    timestamp: float


class RecoverySession:
    """单个账户恢复申请的会话与状态机。

    一般不直接构造，而是通过 :class:`RecoveryManager.create_application`
    创建。时间通过可注入的 ``clock`` 读取，测试无需真实等待。
    """

    def __init__(
        self,
        session_id: str,
        user_id: str,
        target_factor_id: str,
        recovery_code: str,
        created_at: float,
        cooldown_seconds: float,
        validity_seconds: float,
        factor_registry: FactorRegistry,
        clock: Callable[[], float],
    ) -> None:
        self.session_id = session_id
        self.user_id = user_id
        self.target_factor_id = target_factor_id
        self.cooldown_seconds = cooldown_seconds
        self.validity_seconds = validity_seconds
        self._factor_registry = factor_registry
        self._clock = clock
        self._code_hash = self.hash_code(recovery_code)
        self._code_used = False
        self._old_device_confirmed = False
        self._withdrawn = False
        self._recovered = False
        self._expired = False
        # RLock：允许同一线程在持锁状态下调用 events_of 等内部只读方法。
        self._lock = threading.RLock()
        self._events: List[Event] = []
        self._decisions: Dict[str, Decision] = {}
        self._record(
            EventType.CREATED,
            {
                "user_id": user_id,
                "target_factor_id": target_factor_id,
                "cooldown_seconds": cooldown_seconds,
                "validity_seconds": validity_seconds,
            },
            at=created_at,
        )

    # ------------------------------------------------------------------ #
    # 基础只读视图
    # ------------------------------------------------------------------ #
    @property
    def status(self) -> SessionStatus:
        """当前生命周期状态；终态优先，过期只在未完成时生效。"""
        if self._recovered:
            return SessionStatus.RECOVERED
        if self._withdrawn:
            return SessionStatus.WITHDRAWN
        if self._expired:
            return SessionStatus.EXPIRED
        if self._clock() >= self._events[0].timestamp + self.cooldown_seconds:
            return SessionStatus.ACTIVE
        return SessionStatus.PENDING

    @property
    def events(self) -> List[Event]:
        """事件快照（按发生顺序）。"""
        with self._lock:
            return list(self._events)

    @property
    def decisions(self) -> List[Decision]:
        """全部已缓存提交决定的快照，按缓存顺序排列。"""
        with self._lock:
            return list(self._decisions.values())

    @property
    def old_device_confirmed(self) -> bool:
        return self._old_device_confirmed

    @property
    def recovery_code_used(self) -> bool:
        return self._code_used

    def get_decision(self, request_id: str) -> Optional[Decision]:
        """按请求编号取回原决定；没有该编号的提交记录时返回 ``None``。"""
        with self._lock:
            return self._decisions.get(request_id)

    def events_of(self, event_type: EventType) -> List[Event]:
        """筛选某一类型的事件，方便测试与审计。"""
        with self._lock:
            return [e for e in self._events if e.event_type == event_type]

    # ------------------------------------------------------------------ #
    # 流程动作
    # ------------------------------------------------------------------ #
    def confirm_old_device(self) -> Event:
        """旧设备确认。重复确认是幂等的，返回已有的确认事件。"""
        with self._lock:
            if self._withdrawn or self._recovered or self._expired:
                raise RecoveryStateError(
                    f"session {self.session_id} is in final state "
                    f"{self.status.value}"
                )
            if self._old_device_confirmed:
                return self.events_of(EventType.OLD_DEVICE_CONFIRMED)[0]
            self._old_device_confirmed = True
            return self._record(EventType.OLD_DEVICE_CONFIRMED)

    def withdraw(self, reason: str = "user_requested") -> Event:
        """用户撤回。

        撤回发生在重新登记之前：立即把会话置为作废终态，因素登记簿不做
        任何改动，因此原有因素继续可用。已撤回的重复调用幂等返回。
        """
        with self._lock:
            if self._recovered:
                raise RecoveryStateError(
                    "cannot withdraw: recovery has already completed"
                )
            if self._withdrawn:
                return self.events_of(EventType.WITHDRAWN)[0]
            self._withdrawn = True
            return self._record(EventType.WITHDRAWN, {"reason": reason})

    def submit_recovery(
        self,
        request_id: str,
        recovery_code: str,
        new_factor: Factor,
    ) -> Decision:
        """提交恢复码并申请重新登记第二因素。

        整个方法在会话锁内完成“校验 + 落决定 + 替换因素”，因此：

        * 相同 ``request_id`` 的并发重试只有第一个线程执行，其余线程拿到
          同一份缓存决定；
        * 不同 ``request_id`` 的并发提交也被串行化，第一个成功者把会话
          带入终态，后来者必然被拒绝。
        """
        if not request_id or not isinstance(request_id, str):
            raise ValueError("request_id must be a non-empty string")

        with self._lock:
            cached = self._decisions.get(request_id)
            if cached is not None:
                # 幂等：客户端超时重试，取回原决定，绝不二次执行。
                return cached

            now = self._clock()
            created_at = self._events[0].timestamp

            # 1) 撤回优先：撤回立即作废会话，任何后续提交都被挡住。
            if self._withdrawn:
                return self._reject(
                    request_id,
                    REASON_WITHDRAWN,
                    "recovery session has been withdrawn",
                    now,
                )

            # 2) 其他终态。
            if self._recovered:
                return self._reject(
                    request_id,
                    REASON_FINAL_STATE,
                    "recovery session has already completed",
                    now,
                )
            if self._expired or now > created_at + self.validity_seconds:
                if not self._expired:
                    self._expired = True
                    self._record(
                        EventType.EXPIRED,
                        {"validity_seconds": self.validity_seconds},
                        at=now,
                    )
                return self._reject(
                    request_id,
                    REASON_EXPIRED,
                    "recovery session has expired",
                    now,
                )

            # 3) 冷静期未结束。
            if now < created_at + self.cooldown_seconds:
                return self._reject(
                    request_id,
                    REASON_COOLDOWN,
                    "cooldown period has not elapsed",
                    now,
                )

            # 4) 旧设备确认是恢复码生效的前置条件。
            if not self._old_device_confirmed:
                return self._reject(
                    request_id,
                    REASON_NOT_CONFIRMED,
                    "old device confirmation is missing",
                    now,
                )

            # 5) 恢复码一次性：同一会话只能成功使用一次。
            if self._code_used:
                return self._reject(
                    request_id,
                    REASON_CODE_ALREADY_USED,
                    "recovery code has already been used",
                    now,
                )
            if not self._code_matches(recovery_code):
                return self._reject(
                    request_id,
                    REASON_CODE_MISMATCH,
                    "recovery code does not match",
                    now,
                )

            # 6) 只能替换申请中指定的因素，且该因素必须仍然存在。
            existing = self._factor_registry.get_factor(
                self.user_id, self.target_factor_id
            )
            if existing is None:
                return self._reject(
                    request_id,
                    REASON_FACTOR_NOT_FOUND,
                    f"target factor {self.target_factor_id} no longer exists",
                    now,
                )
            if new_factor.factor_id != self.target_factor_id:
                return self._reject(
                    request_id,
                    REASON_TARGET_MISMATCH,
                    "new factor must replace the factor named in the application",
                    now,
                )

            # 全部通过：记录恢复码使用，再执行限定目标的替换。
            self._code_used = True
            self._record(
                EventType.RECOVERY_CODE_USED,
                {"request_id": request_id},
                at=now,
            )
            self._factor_registry.replace_factor(
                self.user_id,
                self.target_factor_id,
                new_factor,
            )
            self._recovered = True
            self._record(
                EventType.REENROLLED,
                {
                    "request_id": request_id,
                    "replaced_factor_id": self.target_factor_id,
                    "new_factor_type": new_factor.factor_type,
                    "new_factor_label": new_factor.label,
                },
                at=now,
            )
            decision = Decision(
                request_id=request_id,
                accepted=True,
                reason=None,
                message="recovery completed",
                session_id=self.session_id,
                new_factor_id=new_factor.factor_id,
                timestamp=now,
            )
            self._decisions[request_id] = decision
            return decision

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _reject(
        self,
        request_id: str,
        reason: str,
        message: str,
        now: float,
    ) -> Decision:
        """落一条拒绝决定并追加对应的拒绝事件。"""
        self._record(
            EventType.REENROLLMENT_REJECTED,
            {"request_id": request_id, "reason": reason, "message": message},
            at=now,
        )
        decision = Decision(
            request_id=request_id,
            accepted=False,
            reason=reason,
            message=message,
            session_id=self.session_id,
            new_factor_id=None,
            timestamp=now,
        )
        self._decisions[request_id] = decision
        return decision

    def _record(
        self,
        event_type: EventType,
        detail: Optional[Dict[str, Any]] = None,
        at: Optional[float] = None,
    ) -> Event:
        event = Event(
            seq=len(self._events) + 1,
            event_type=event_type,
            timestamp=self._clock() if at is None else at,
            detail=dict(detail or {}),
        )
        self._events.append(event)
        return event

    def _code_matches(self, code: str) -> bool:
        candidate = self.hash_code(code)
        return hmac.compare_digest(candidate, self._code_hash)

    @staticmethod
    def hash_code(code: str) -> str:
        """恢复码只以 SHA-256 摘要形式保存在会话里。"""
        return hashlib.sha256(code.encode("utf-8")).hexdigest()


class ManualClock:
    """测试用可控时钟，默认从固定时间点起步，可前进任意秒数。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move clock backwards")
        self._now += seconds


class RecoveryManager:
    """恢复会话的创建入口与查询入口。"""

    def __init__(
        self,
        factor_registry: FactorRegistry,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        import time

        self._factor_registry = factor_registry
        self._clock: Callable[[], float] = clock or time.time
        self._sessions: Dict[str, RecoverySession] = {}
        self._guard = threading.Lock()

    def create_application(
        self,
        user_id: str,
        target_factor_id: str,
        recovery_code: str,
        cooldown_seconds: float = 3600.0,
        validity_seconds: float = 86400.0,
    ) -> RecoverySession:
        """创建一笔恢复申请，立即进入冷静期。

        ``target_factor_id`` 必须是用户登记簿上现存的因素，否则抛出
        :class:`LookupError`，避免产生无法完成替换的无效申请。
        """
        if cooldown_seconds < 0 or validity_seconds < 0:
            raise ValueError("durations must be non-negative")
        if validity_seconds < cooldown_seconds:
            raise ValueError("validity_seconds must be >= cooldown_seconds")
        if self._factor_registry.get_factor(user_id, target_factor_id) is None:
            raise LookupError(
                f"target factor not found for user {user_id}: {target_factor_id}"
            )

        session_id = uuid.uuid4().hex
        session = RecoverySession(
            session_id=session_id,
            user_id=user_id,
            target_factor_id=target_factor_id,
            recovery_code=recovery_code,
            created_at=self._clock(),
            cooldown_seconds=cooldown_seconds,
            validity_seconds=validity_seconds,
            factor_registry=self._factor_registry,
            clock=self._clock,
        )
        with self._guard:
            self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> RecoverySession:
        """按 ID 取会话，不存在时抛出 :class:`UnknownSessionError`。"""
        with self._guard:
            session = self._sessions.get(session_id)
        if session is None:
            from .errors import UnknownSessionError

            raise UnknownSessionError(f"unknown recovery session: {session_id}")
        return session

    def list_sessions(self, user_id: Optional[str] = None) -> List[RecoverySession]:
        """列出会话，可按用户过滤；顺序按创建时间（事件序号）稳定排列。"""
        with self._guard:
            sessions = list(self._sessions.values())
        if user_id is not None:
            sessions = [s for s in sessions if s.user_id == user_id]
        sessions.sort(key=lambda s: s.events[0].timestamp)
        return sessions
