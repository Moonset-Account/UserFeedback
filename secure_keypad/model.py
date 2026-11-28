"""安全输入组件的事件模型。

场景：自助设备数字键每次输入一位后重排；读屏播报可能滞后于触摸位置。
本模块实现一个防御性事件模型：

* 布局每次发布都带单调递增的版本号；
* 支持触摸（按下/松开配对）和无障碍（焦点 -> 播报完成 -> 确认）两种输入通道；
* 播报完成、确认、取消、通道切换可以任意交错到达，模型只接受
  “本次确实展示或读出的那个键”的确认；
* 一次手势或一次焦点确认最多提交一位；
* 切换到下一人（新会话）后，旧会话的播报回调不可恢复输入；
* 调用方只收到不含数字的确认事件和受保护的结果句柄；
* 诊断（审计）记录中不出现输入数字。
"""

from __future__ import annotations

import secrets
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Deque, Optional, Tuple


# ---------------------------------------------------------------------------
# 通道与拒绝原因
# ---------------------------------------------------------------------------


class Channel(Enum):
    """输入通道。"""

    TOUCH = "touch"
    ACCESSIBILITY = "accessibility"


class RejectReason(Enum):
    """确认被拒绝的原因（不含任何输入数字）。"""

    NO_ACTIVE_GESTURE = "no_active_gesture"
    KEY_MISMATCH = "key_mismatch"
    STALE_LAYOUT = "stale_layout"
    UNKNOWN_KEY = "unknown_key"
    NO_ACTIVE_FOCUS = "no_active_focus"
    ANNOUNCEMENT_PENDING = "announcement_pending"
    STALE_ANNOUNCEMENT = "stale_announcement"


# ---------------------------------------------------------------------------
# 布局（每次发布带版本）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """一次发布的键盘布局：版本号 + 键位到数字的映射。

    布局是展示层信息（屏幕上可见），因此可以向调用方公开；
    它绝不包含“已输入”的信息。
    """

    version: int
    positions: Tuple[Tuple[str, str], ...]  # ((key_id, digit), ...)

    def __post_init__(self) -> None:
        seen = set()
        for key_id, digit in self.positions:
            if key_id in seen:
                raise ValueError(f"duplicate key id: {key_id!r}")
            seen.add(key_id)
            if not (isinstance(digit, str) and len(digit) == 1 and digit.isdigit()):
                raise ValueError(f"invalid digit for key {key_id!r}: {digit!r}")

    @property
    def key_ids(self) -> Tuple[str, ...]:
        return tuple(key_id for key_id, _ in self.positions)

    def has_key(self, key_id: str) -> bool:
        return any(k == key_id for k, _ in self.positions)

    def digit_for(self, key_id: str) -> str:
        for k, d in self.positions:
            if k == key_id:
                return d
        raise KeyError(key_id)


LayoutProvider = Callable[[int], Layout]


def _default_provider(key_ids: Tuple[str, ...]) -> LayoutProvider:
    """默认布局提供者：每次用加密安全随机源重排数字。"""
    rng = secrets.SystemRandom()
    base = list("0123456789")[: len(key_ids)]

    def provide(version: int) -> Layout:
        shuffled = base[:]
        rng.shuffle(shuffled)
        return Layout(version, tuple(zip(key_ids, shuffled)))

    return provide


# ---------------------------------------------------------------------------
# 播报令牌（无障碍通道）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnnouncementToken:
    """焦点播报令牌。

    焦点落到某个键时由组件签发，读屏完成回调必须原样带回。
    令牌绑定 (会话, 布局版本, 焦点序号, 键位)，任何一个维度过期即作废。
    """

    session: int
    layout_version: int
    focus_seq: int
    key_id: str


# ---------------------------------------------------------------------------
# 结果侧事件（调用方可见，绝不包含输入数字）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionStarted:
    session: int


@dataclass(frozen=True)
class DigitConfirmed:
    """一位输入被确认。只携带计数，不携带数字本身。"""

    session: int
    digit_count: int
    channel: Channel


@dataclass(frozen=True)
class ConfirmRejected:
    session: int
    reason: RejectReason
    channel: Channel


# ---------------------------------------------------------------------------
# 展示侧事件（供 UI/读屏渲染，含待展示/待播报的键名）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutPublished:
    session: int
    layout: Layout


@dataclass(frozen=True)
class AnnouncementRequested:
    """请求读屏播报。label 是“本次要读出的键名”，属于展示层信息。"""

    session: int
    token: AnnouncementToken
    label: str


# ---------------------------------------------------------------------------
# 诊断（审计）记录：不出现输入数字
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    session: int
    kind: str
    channel: Optional[str] = None
    layout_version: Optional[int] = None
    key_id: Optional[str] = None
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# 受保护的结果句柄
# ---------------------------------------------------------------------------


class SealedResult:
    """封存后的输入结果：不可读出数字，只能做等值比较。"""

    __slots__ = ("_digits",)

    def __init__(self, digits) -> None:
        self._digits = tuple(digits)

    def __len__(self) -> int:
        return len(self._digits)

    def __eq__(self, other) -> bool:
        return isinstance(other, SealedResult) and self._digits == other._digits

    def __hash__(self) -> int:
        return hash(self._digits)

    def __repr__(self) -> str:
        return f"SealedResult(length={len(self._digits)}, digits=***)"

    __str__ = __repr__


class ResultHandle:
    """受保护的结果句柄。

    调用方只能查询位数、封存结果；数字不以任何形式经 repr/str 暴露。
    """

    __slots__ = ("_session", "_digits")

    def __init__(self, session: int) -> None:
        self._session = session
        self._digits: list[str] = []

    @property
    def session(self) -> int:
        return self._session

    @property
    def digit_count(self) -> int:
        return len(self._digits)

    def _append(self, digit: str) -> None:
        self._digits.append(digit)

    def seal(self) -> SealedResult:
        return SealedResult(self._digits)

    def __repr__(self) -> str:
        return (
            f"ResultHandle(session={self._session}, "
            f"digit_count={len(self._digits)}, digits=***)"
        )

    __str__ = __repr__


# ---------------------------------------------------------------------------
# 内部待决状态
# ---------------------------------------------------------------------------


@dataclass
class _Gesture:
    session: int
    layout_version: int
    key_id: str


@dataclass
class _Focus:
    token: AnnouncementToken
    announced: bool = False


# ---------------------------------------------------------------------------
# 组件主体
# ---------------------------------------------------------------------------


class SecureKeypad:
    """安全输入组件的事件模型。

    使用顺序：begin_session() -> 渲染 current_layout() -> 接收触摸/无障碍
    事件 -> poll_events() 取确认/拒绝事件 -> result() 取受保护句柄。
    """

    def __init__(
        self,
        layout_provider: Optional[LayoutProvider] = None,
        key_ids: Optional[Tuple[str, ...]] = None,
    ) -> None:
        self._key_ids: Tuple[str, ...] = (
            tuple(key_ids) if key_ids is not None else tuple(f"K{i}" for i in range(10))
        )
        self._provider: LayoutProvider = layout_provider or _default_provider(
            self._key_ids
        )
        self._session = 0
        self._layout: Optional[Layout] = None
        self._channel = Channel.TOUCH
        self._gesture: Optional[_Gesture] = None
        self._focus: Optional[_Focus] = None
        self._focus_seq = 0
        self._result: Optional[ResultHandle] = None
        self._events: Deque[object] = deque()
        self._display: Deque[object] = deque()
        self._audit_records: list[AuditRecord] = []
        self._audit_seq = 0

    # -- 会话管理 ---------------------------------------------------------

    def begin_session(self, user_tag: Optional[str] = None) -> None:
        """开始（或切换到下一人）新会话。

        旧会话的手势、焦点、播报令牌全部作废；结果句柄重新生成；
        布局重新发布（版本继续递增）。user_tag 仅作调用方标识，
        出于隐私考虑不写入诊断记录。
        """
        del user_tag  # 故意不记录，避免诊断日志出现个人标识
        self._session += 1
        self._gesture = None
        self._focus = None
        self._result = ResultHandle(self._session)
        self._republish_layout()
        self._audit("session_started")
        self._events.append(SessionStarted(self._session))

    # switch_user 语义与 begin_session 相同：切到下一人即新会话。
    def switch_user(self, user_tag: Optional[str] = None) -> None:
        self.begin_session(user_tag)

    # -- 状态查询 ---------------------------------------------------------

    def current_layout(self) -> Layout:
        self._require_session()
        assert self._layout is not None
        return self._layout

    def result(self) -> ResultHandle:
        self._require_session()
        assert self._result is not None
        return self._result

    @property
    def channel(self) -> Channel:
        return self._channel

    # -- 通道切换 ---------------------------------------------------------

    def set_channel(self, channel: Channel) -> None:
        """切换输入通道；另一通道的待决状态立即作废。"""
        self._require_session()
        channel = Channel(channel)
        if channel is self._channel:
            return
        self._gesture = None
        self._focus = None
        self._channel = channel
        self._audit("channel_switched", channel=channel.value)

    # -- 触摸通道 ---------------------------------------------------------

    def touch_down(self, key_id: str) -> None:
        self._require_session()
        if self._channel is not Channel.TOUCH:
            self._audit("touch_down_ignored", key_id=key_id, reason="wrong_channel")
            return
        assert self._layout is not None
        if not self._layout.has_key(key_id):
            self._audit("touch_down_ignored", key_id=key_id, reason="unknown_key")
            return
        if self._gesture is not None:
            self._audit("touch_down_ignored", key_id=key_id, reason="gesture_active")
            return
        self._gesture = _Gesture(self._session, self._layout.version, key_id)
        self._audit("gesture_started", key_id=key_id)

    def touch_up(self, key_id: str) -> None:
        self._require_session()
        gesture, self._gesture = self._gesture, None
        if gesture is None:
            self._reject(RejectReason.NO_ACTIVE_GESTURE, Channel.TOUCH)
            return
        assert self._layout is not None
        if (
            gesture.session != self._session
            or gesture.layout_version != self._layout.version
        ):
            self._reject(RejectReason.STALE_LAYOUT, Channel.TOUCH)
            return
        if gesture.key_id != key_id or not self._layout.has_key(key_id):
            self._reject(RejectReason.KEY_MISMATCH, Channel.TOUCH)
            return
        self._confirm(gesture.key_id, Channel.TOUCH)

    def touch_cancel(self) -> None:
        self._require_session()
        if self._gesture is not None:
            self._gesture = None
            self._audit("gesture_cancelled")

    # -- 无障碍通道 ---------------------------------------------------------

    def focus_key(self, key_id: str) -> None:
        """无障碍焦点移动到某个键，签发播报令牌并请求读屏。"""
        self._require_session()
        if self._channel is not Channel.ACCESSIBILITY:
            self._audit("focus_ignored", key_id=key_id, reason="wrong_channel")
            return
        assert self._layout is not None
        if not self._layout.has_key(key_id):
            self._audit("focus_ignored", key_id=key_id, reason="unknown_key")
            return
        self._focus_seq += 1
        token = AnnouncementToken(
            self._session, self._layout.version, self._focus_seq, key_id
        )
        self._focus = _Focus(token)
        label = self._layout.digit_for(key_id)  # 仅进入展示侧事件
        self._display.append(AnnouncementRequested(self._session, token, label))
        self._audit("focus_moved", key_id=key_id)

    def announcement_done(self, token: AnnouncementToken) -> None:
        """读屏播报完成回调。过期/被取代/跨会话的令牌一律作废。"""
        self._require_session()
        focus = self._focus
        assert self._layout is not None
        if (
            focus is None
            or focus.token != token
            or token.session != self._session
            or token.layout_version != self._layout.version
        ):
            self._audit(
                "announcement_stale", key_id=getattr(token, "key_id", None)
            )
            return
        focus.announced = True
        self._audit("announcement_completed", key_id=token.key_id)

    def confirm_focus(self) -> None:
        """确认当前焦点键。仅当本次焦点的播报已在当前布局版本下完成才接受。"""
        self._require_session()
        focus = self._focus
        if focus is None:
            self._reject(RejectReason.NO_ACTIVE_FOCUS, Channel.ACCESSIBILITY)
            return
        assert self._layout is not None
        if (
            focus.token.session != self._session
            or focus.token.layout_version != self._layout.version
        ):
            self._reject(RejectReason.STALE_ANNOUNCEMENT, Channel.ACCESSIBILITY)
            return
        if not focus.announced:
            self._reject(RejectReason.ANNOUNCEMENT_PENDING, Channel.ACCESSIBILITY)
            return
        self._confirm(focus.token.key_id, Channel.ACCESSIBILITY)

    def cancel_focus(self) -> None:
        self._require_session()
        if self._focus is not None:
            self._focus = None
            self._audit("focus_cancelled")

    # -- 事件与诊断 ---------------------------------------------------------

    def poll_events(self) -> list:
        """取走结果侧事件（SessionStarted / DigitConfirmed / ConfirmRejected）。"""
        events = list(self._events)
        self._events.clear()
        return events

    def poll_display(self) -> list:
        """取走展示侧事件（LayoutPublished / AnnouncementRequested）。"""
        events = list(self._display)
        self._display.clear()
        return events

    def audit_log(self) -> Tuple[AuditRecord, ...]:
        return tuple(self._audit_records)

    # -- 内部 ----------------------------------------------------------------

    def _require_session(self) -> None:
        if self._layout is None:
            raise RuntimeError("no active session; call begin_session() first")

    def _confirm(self, key_id: str, channel: Channel) -> None:
        assert self._layout is not None and self._result is not None
        digit = self._layout.digit_for(key_id)
        self._result._append(digit)
        # 一次手势/焦点确认最多一位：确认后立即清空全部待决状态。
        self._gesture = None
        self._focus = None
        self._events.append(
            DigitConfirmed(self._session, self._result.digit_count, channel)
        )
        # 诊断记录只写键位与版本，绝不写数字。
        self._audit("input_confirmed", channel=channel.value, key_id=key_id)
        # 每输入一位后重排布局（发布新版本）。
        self._republish_layout()

    def _reject(self, reason: RejectReason, channel: Channel) -> None:
        self._events.append(ConfirmRejected(self._session, reason, channel))
        self._audit("confirm_rejected", channel=channel.value, reason=reason.value)

    def _republish_layout(self) -> None:
        next_version = 0 if self._layout is None else self._layout.version + 1
        layout = self._provider(next_version)
        if layout.version != next_version:
            raise ValueError(
                f"layout provider returned version {layout.version}, "
                f"expected {next_version}"
            )
        if sorted(layout.key_ids) != sorted(self._key_ids):
            raise ValueError("layout provider returned mismatched key ids")
        self._layout = layout
        self._display.append(LayoutPublished(self._session, layout))
        self._audit("layout_published")

    def _audit(
        self,
        kind: str,
        *,
        channel: Optional[str] = None,
        key_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        self._audit_seq += 1
        self._audit_records.append(
            AuditRecord(
                seq=self._audit_seq,
                session=self._session,
                kind=kind,
                channel=channel,
                layout_version=self._layout.version if self._layout else None,
                key_id=key_id,
                reason=reason,
            )
        )
