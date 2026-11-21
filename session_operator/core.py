"""增量会话聚合算子。

语义摘要
--------
* 同一设备的事件按事件时间排序，相邻事件间隔 ``<= gap_seconds``（默认 10 秒）
  时属于同一会话。会话恰好在 ``last_time + gap_seconds`` 处封存。
* 会话字段：设备、首末时间、事件条数、金额和。
* 输入只有三种：带唯一编号的插入、按编号撤回、单调水位。
* 水位 ``<= watermark`` 的事件不可再插入或撤回；会话若满足
  ``last_time + gap_seconds <= watermark`` 则已封存，永不撤回。
* 每次操作只发布受影响设备的变化：最多一次旧结果撤回加一次新结果插入，
  未受影响的设备不产生任何 changelog。

实现方式
--------
每个设备保存全部可变更事件；每次变更后用滑动窗口重算该设备的会话集合，
再与“已发布未撤回”的结果集合做差量。由于会话最多把两个旧会话合并成一个、
或把一个旧会话拆成两个，单设备每次变更的差量规模为常数，符合增量的定位。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .errors import (
    DuplicateEventId,
    SealedSessionViolation,
    UnknownEventId,
    WatermarkRegression,
    WatermarkViolation,
)
from .model import (
    DEFAULT_GAP_SECONDS,
    Change,
    ChangeKind,
    Event,
    SessionResult,
)

_EPSILON = 1e-9


def _result_sort_key(result: SessionResult) -> Tuple[float, float, str]:
    return (result.first_time, result.last_time, result.device)


def _change_sort_key(change: Change) -> Tuple[int, str, float, float]:
    # 同一批次内先全部撤回、再全部插入；各自按 (设备, 首时间, 末时间) 排列。
    return (
        0 if change.kind is ChangeKind.RETRACT else 1,
        change.result.device,
        change.result.first_time,
        change.result.last_time,
    )


class SessionOperator:
    """按设备聚合会话的增量算子。

    ``apply`` 类方法一次只接受一个输入并返回本次产生的 changelog；这类方法在
    被拒绝时抛异常且不留下任何副作用。``insert`` / ``retract`` / ``advance_watermark``
    是它们的语义化别名。
    """

    def __init__(self, gap_seconds: float = DEFAULT_GAP_SECONDS) -> None:
        if not isinstance(gap_seconds, (int, float)) or isinstance(gap_seconds, bool):
            raise TypeError("gap_seconds 必须是数字")
        gap = float(gap_seconds)
        if not math.isfinite(gap) or gap < 0:
            raise ValueError("gap_seconds 必须是有限非负数")
        self._gap = gap
        self._watermark: Optional[float] = None
        # 设备 -> {事件编号: Event}
        self._events: Dict[str, Dict[str, Event]] = defaultdict(dict)
        # 设备 -> 已发布且尚未撤回的结果
        self._published: Dict[str, Set[SessionResult]] = defaultdict(set)
        # 编号 -> Event，编号全局唯一，用于按编号撤回与重复检测
        self._events_by_id: Dict[str, Event] = {}

    # ------------------------------------------------------------------ 只读属性
    @property
    def gap_seconds(self) -> float:
        return self._gap

    @property
    def watermark(self) -> Optional[float]:
        """当前水位；``None`` 表示尚未收到水位。"""
        return self._watermark

    def is_sealed(self, last_time: float) -> bool:
        """判断以 ``last_time`` 为末尾的会话是否已封存。

        封存条件是 ``last_time + gap_seconds <= watermark``，即水位达到会话末尾
        之后 ``gap_seconds``。尚未收到水位时任何会话都未封存。
        """
        return self._watermark is not None and last_time + self._gap <= self._watermark + _EPSILON

    def events_for(self, device: str) -> List[Event]:
        """当前仍可变更的该设备事件（按事件时间排序）。"""
        return sorted(
            self._events.get(device, {}).values(),
            key=lambda event: (event.time, event.event_id),
        )

    def devices(self) -> List[str]:
        """当前有事件或已发布结果的设备（按名称排序）。"""
        return sorted(set(self._events) | set(self._published))

    def published_results(self, device: str) -> List[SessionResult]:
        """该设备已发布且尚未撤回的结果（稳定排序）。"""
        return sorted(self._published.get(device, ()), key=_result_sort_key)

    def all_published_results(self) -> List[SessionResult]:
        results: List[SessionResult] = []
        for device in self._published:
            results.extend(self._published[device])
        return sorted(results, key=_result_sort_key)

    def materialized_results(self, device: str) -> List[SessionResult]:
        """由当前事件重算出的该设备会话（用于核对与恢复）。"""
        return self._compute_sessions(device)

    # ------------------------------------------------------------------ 输入入口
    def apply(self, item: object, watermark: Optional[float] = None) -> List[Change]:
        """接受一次输入并返回本次的 changelog。

        支持三种写法：

        * ``apply(Event(...))``
        * ``apply(("insert", event_id, device, time, amount))``
        * ``apply(("retract", event_id))``
        * ``apply(("watermark", value))``
        * ``apply(..., watermark=value)``：原子地“按水位推进后执行本次操作”，
          供上游在同一批次内同送事件与水位时使用。
        """
        if watermark is not None and not isinstance(item, tuple):
            raise TypeError("同时给出 watermark 时，item 必须是 (kind, ...) 元组")
        if isinstance(item, Event):
            if watermark is not None:
                raise TypeError("Event 输入不能与 watermark 关键字同时使用")
            return self.insert(item)
        if not isinstance(item, tuple) or not item:
            raise TypeError(f"无法识别的输入：{item!r}")

        kind = item[0]
        if not isinstance(kind, str):
            raise TypeError(f"输入动作必须是字符串：{kind!r}")
        kind = kind.lower()

        if kind == "watermark":
            if watermark is not None:
                raise TypeError("同一输入不能给出两个水位")
            if len(item) != 2:
                raise TypeError("watermark 输入形如 ('watermark', value)")
            return self.advance_watermark(item[1])
        if kind in ("insert", "retract"):
            if watermark is None:
                return self._dispatch_plain(kind, item)
            return self._apply_with_watermark(watermark, kind, item)
        raise TypeError(f"无法识别的输入动作：{item!r}")

    def _dispatch_plain(self, kind: str, item: tuple) -> List[Change]:
        if kind == "insert":
            if len(item) != 5:
                raise TypeError("insert 输入形如 ('insert', id, device, time, amount)")
            _, event_id, device, time, amount = item
            return self.insert(event_id, device, time, amount)
        if len(item) != 2:
            raise TypeError("retract 输入形如 ('retract', id)")
        return self.retract(item[1])

    def insert(
        self,
        event: Event | str,
        device: Optional[str] = None,
        time: Optional[float] = None,
        amount: Optional[float] = None,
        *,
        watermark: Optional[float] = None,
    ) -> List[Change]:
        """插入一条事件，返回受影响设备的 changelog。

        既接受 ``insert(Event(...))``，也接受
        ``insert(event_id, device, time, amount)``。可选的 ``watermark`` 参数会在
        执行插入前后先推进水位（同一批次内先推进水位再插入，从而正确执行水位约束）。
        """
        if isinstance(event, Event):
            if any(value is not None for value in (device, time, amount)):
                raise TypeError("给出 Event 时不能再传 device/time/amount")
            return self._insert_impl(event, watermark)
        if device is None or time is None or amount is None:
            raise TypeError("insert 需要 (event_id, device, time, amount)")
        return self._insert_impl(Event(event, device, time, amount), watermark)

    def retract(self, event_or_id: Event | str, *, watermark: Optional[float] = None) -> List[Change]:
        """按编号撤回一条事件，返回受影响设备的 changelog。"""
        event_id = event_or_id.event_id if isinstance(event_or_id, Event) else event_or_id
        if not isinstance(event_id, str):
            raise TypeError("撤回需要字符串事件编号")
        return self._retract_impl(event_id, watermark)

    def advance_watermark(self, value: float) -> List[Change]:
        """单调推进水位并返回因会话封存而无需变更的 changelog（当前实现恒为空）。

        水位推进本身不会改变任何会话内容，因此返回空列表；保留返回值是为了让
        调用方统一处理 ``List[Change]``。
        """
        previous = self._watermark
        new_watermark = self._validate_watermark(value)
        if previous is not None and new_watermark < previous:
            raise WatermarkRegression(previous, new_watermark)
        self._watermark = new_watermark
        return []

    # ------------------------------------------------------------------ 内部实现
    def _apply_with_watermark(self, new_watermark: float, kind: str, item: tuple) -> List[Change]:
        """先推进水位再执行操作；任一环节被拒绝则整体回滚，不留下副作用。"""
        previous = self._watermark
        value = self._validate_watermark(new_watermark)
        if previous is not None and value < previous:
            raise WatermarkRegression(previous, value)
        self._watermark = value
        try:
            if kind == "insert":
                if len(item) != 5:
                    raise TypeError("insert 输入形如 ('insert', id, device, time, amount)")
                _, event_id, device, time, amount = item
                if not isinstance(event_id, str) or not isinstance(device, str):
                    raise TypeError("insert 的编号与设备必须是字符串")
                return self._insert_impl(Event(event_id, device, time, amount), None)
            if len(item) != 2:
                raise TypeError("retract 输入形如 ('retract', id)")
            event_id = item[1]
            if not isinstance(event_id, str):
                raise TypeError("retract 的事件编号必须是字符串")
            return self._retract_impl(event_id, None)
        except BaseException:
            self._watermark = previous
            raise

    def _validate_watermark(self, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"水位必须是数字：{value!r}")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"水位必须是有限数：{value!r}")
        return result

    def _insert_impl(self, event: Event, watermark: Optional[float]) -> List[Change]:
        previous_watermark = self._watermark
        if watermark is not None:
            value = self._validate_watermark(watermark)
            if previous_watermark is not None and value < previous_watermark:
                raise WatermarkRegression(previous_watermark, value)
            self._watermark = value
        try:
            return self._do_insert(event)
        except BaseException:
            if watermark is not None:
                self._watermark = previous_watermark
            raise

    def _retract_impl(self, event_id: str, watermark: Optional[float]) -> List[Change]:
        previous_watermark = self._watermark
        if watermark is not None:
            value = self._validate_watermark(watermark)
            if previous_watermark is not None and value < previous_watermark:
                raise WatermarkRegression(previous_watermark, value)
            self._watermark = value
        try:
            return self._do_retract(event_id)
        except BaseException:
            if watermark is not None:
                self._watermark = previous_watermark
            raise

    def _do_insert(self, event: Event) -> List[Change]:
        if not isinstance(event, Event):
            raise TypeError("insert 需要 Event 实例")
        if not isinstance(event.event_id, str) or not isinstance(event.device, str):
            raise TypeError("事件编号与设备必须是字符串")
        event_id = event.event_id
        if event_id in self._events_by_id:
            raise DuplicateEventId(event_id)
        time = self._validate_event_time(event.time)
        amount = self._validate_amount(event.amount)
        # 先做与状态无关的编号校验，再检查水位/封存，最后才写入。
        if self._watermark is not None and time <= self._watermark:
            raise WatermarkViolation(event_id, time)

        normalized = Event(event_id, event.device, time, amount)
        self._events[event.device][event_id] = normalized
        self._events_by_id[event_id] = normalized
        try:
            return self._publish(self._publish_diff(event.device))
        except BaseException:
            del self._events[event.device][event_id]
            if not self._events[event.device]:
                del self._events[event.device]
            del self._events_by_id[event_id]
            raise

    def _do_retract(self, event_id: str) -> List[Change]:
        if not isinstance(event_id, str):
            raise TypeError("撤回需要字符串事件编号")
        event = self._events_by_id.get(event_id)
        if event is None:
            raise UnknownEventId(event_id)
        device = event.device
        if self._watermark is not None:
            _, last_time = self._session_bounds(device, event)
            if self.is_sealed(last_time):
                raise SealedSessionViolation(event_id, event.time)
            if event.time <= self._watermark:
                # 会话尚未封存（水位 < last + gap），但该事件时间已在水位及以前，
                # 按“水位及以前不再接受修改”拒绝。
                raise WatermarkViolation(event_id, event.time)

        del self._events[device][event_id]
        del self._events_by_id[event_id]
        try:
            changes = self._publish_diff(device)
        except BaseException:
            self._events[device][event_id] = event
            self._events_by_id[event_id] = event
            raise
        if not self._events[device]:
            del self._events[device]
        return self._publish(changes)

    @staticmethod
    def _validate_event_time(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"事件时间必须是数字：{value!r}")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"事件时间必须是有限数：{value!r}")
        return result

    @staticmethod
    def _validate_amount(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"金额必须是数字：{value!r}")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"金额必须是有限数：{value!r}")
        return result

    def _session_bounds(self, device: str, event: Event) -> Tuple[float, float]:
        """包含 ``event`` 的会话（按当前事件重算）的首末时间。"""
        for session in self._compute_sessions(device):
            if session.first_time <= event.time <= session.last_time:
                return session.first_time, session.last_time
        raise AssertionError(f"事件 {event.event_id!r} 未落入任何会话")  # pragma: no cover

    def _compute_sessions(self, device: str) -> List[SessionResult]:
        events = self.events_for(device)
        sessions: List[SessionResult] = []
        current: List[Event] = []
        total = 0.0
        previous_time: Optional[float] = None
        for event in events:
            if previous_time is not None and event.time - previous_time > self._gap + _EPSILON:
                sessions.append(self._make_result(current, total))
                current = []
                total = 0.0
            current.append(event)
            total += event.amount
            previous_time = event.time
        if current:
            sessions.append(self._make_result(current, total))
        return sessions

    @staticmethod
    def _make_result(events: List[Event], total: float) -> SessionResult:
        return SessionResult(
            device=events[0].device,
            first_time=events[0].time,
            last_time=events[-1].time,
            event_count=len(events),
            amount_sum=total,
        )

    def _publish_diff(self, device: str) -> List[Change]:
        """重算该设备会话并与已发布结果求差量（不修改状态）。"""
        current = set(self._compute_sessions(device))
        published = self._published.get(device, set())
        old_results = sorted(published - current, key=_result_sort_key)
        new_results = sorted(current - published, key=_result_sort_key)
        changes = [Change(ChangeKind.RETRACT, result) for result in old_results]
        changes.extend(Change(ChangeKind.INSERT, result) for result in new_results)
        return changes

    def _publish(self, changes: Iterable[Change]) -> List[Change]:
        """把差量应用到已发布集合并按稳定顺序返回。"""
        collected = list(changes)
        for change in collected:
            bucket = self._published[change.result.device]
            if change.kind is ChangeKind.RETRACT:
                bucket.discard(change.result)
            else:
                bucket.add(change.result)
        return sorted(collected, key=_change_sort_key)
