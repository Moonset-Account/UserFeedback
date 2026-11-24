"""当地挂钟时刻到 UTC 时刻的映射。

控制器只依赖 :class:`LocalTimeMap` 协议，不直接绑定 ``zoneinfo`` 或
``pytz``，因此可以在测试中注入完全确定的合成时区，精确覆盖春令时
前拨、秋令时回拨等边界。

约定
----
* 输入的当地时间是 *naive* :class:`~datetime.datetime`（只表示挂钟
  时间，不带 tzinfo）；
* 返回的 UTC 时刻是带 ``tzinfo=timezone.utc`` 的 aware datetime；
* 正常挂钟时刻映射到恰好一个 UTC 时刻；
* 回拨产生的歧义时刻映射到两个 UTC 时刻，顺序为
  ``[夏令时那一遍, 标准时那一遍]``，即按真实发生时间升序；
* 前拨跳过的不存在时刻返回空元组。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

try:  # zoneinfo 自 Python 3.9 起为标准库
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - 本项目要求 Python 3.11
    ZoneInfo = None  # type: ignore[assignment]


@runtime_checkable
class LocalTimeMap(Protocol):
    """当地挂钟时刻 → 0~2 个 UTC 时刻的映射协议。"""

    def resolve(self, local_wall_time: datetime) -> tuple[datetime, ...]:
        """解析一个 naive 当地挂钟时刻。

        返回 0 个（换表跳过）、1 个（普通时刻）或 2 个（回拨歧义，
        按真实发生时间升序）aware UTC datetime。
        """
        ...

    def to_local(self, utc_instant: datetime) -> datetime:
        """把 aware UTC 时刻转成该时区的 naive 当地挂钟时刻。"""
        ...


@dataclass(frozen=True)
class Transition:
    """合成时区的一次换表：``at_utc`` 起 UTC 偏移变为 ``new_offset``。"""

    at_utc: datetime
    new_offset: timedelta

    def __post_init__(self) -> None:
        if self.at_utc.tzinfo is None:
            raise ValueError("换表时刻 at_utc 必须是带时区的 UTC 时刻")


class SyntheticZone:
    """由若干换表点定义的合成时区，便于构造确定性测试数据。

    典型用法（模拟偏移在 -4/-5 之间切换、每次各跳一小时的时区）::

        zone = SyntheticZone(
            name="Synthetic/America",
            initial_offset=timedelta(hours=-5),
            transitions=[
                # 春令时：UTC 06:00 起偏移变为 -4，
                # 当地标准时 01:00 -> 夏令时 02:00（01:00~02:00 不存在）
                Transition(datetime(2026, 3, 8, 6, tzinfo=utc),
                           timedelta(hours=-4)),
                # 秋令时：UTC 06:00 起偏移变回 -5，
                # 当地夏令时 02:00 -> 标准时 01:00（01 点档出现两次）
                Transition(datetime(2026, 11, 1, 6, tzinfo=utc),
                           timedelta(hours=-5)),
            ],
        )

    偏移按换表点分段确定。``resolve`` 用各历史偏移逆推 UTC 后做
    “UTC 转回本地挂钟时间”的回转校验：普通时刻去重后剩 1 个，回拨
    歧义得到 2 个，前拨缺口没有任何偏移能回转成原挂钟时间，得到 0 个。
    """

    def __init__(
        self,
        name: str,
        initial_offset: timedelta,
        transitions: list[Transition] | tuple[Transition, ...] = (),
    ) -> None:
        self.name = name
        self._initial_offset = initial_offset
        self._transitions = tuple(sorted(transitions, key=lambda t: t.at_utc))
        for t in self._transitions:
            if not isinstance(t, Transition):
                raise TypeError("transitions 必须是 Transition 实例")

    def offset_at(self, utc_instant: datetime) -> timedelta:
        """返回某个 UTC 时刻上本时区生效的 UTC 偏移。"""
        self._require_aware_utc(utc_instant)
        offset = self._initial_offset
        for transition in self._transitions:
            if utc_instant >= transition.at_utc:
                offset = transition.new_offset
            else:
                break
        return offset

    def resolve(self, local_wall_time: datetime) -> tuple[datetime, ...]:
        if local_wall_time.tzinfo is not None:
            raise ValueError("local_wall_time 必须是不带 tzinfo 的挂钟时间")

        # 用历史上出现过的每个偏移各做一次“挂钟时间 - 偏移 = UTC”
        # 的逆推，再把 UTC 转回本地挂钟时间做回转校验：
        # * 普通时刻：两个偏移逆推后回转结果相同，去重后剩 1 个；
        # * 回拨歧义：两个偏移都能回转成原挂钟时间，得到 2 个；
        # * 前拨缺口：没有任何偏移能回转成原挂钟时间，得到 0 个。
        candidate_offsets = {self._initial_offset}
        for transition in self._transitions:
            candidate_offsets.add(transition.new_offset)

        instants: list[datetime] = []
        for offset in candidate_offsets:
            instant = (local_wall_time - offset).replace(tzinfo=timezone.utc)
            if self.to_local(instant) == local_wall_time and instant not in instants:
                instants.append(instant)

        return tuple(sorted(instants))

    def to_local(self, utc_instant: datetime) -> datetime:
        self._require_aware_utc(utc_instant)
        return (utc_instant + self.offset_at(utc_instant)).replace(tzinfo=None)

    @staticmethod
    def _require_aware_utc(value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("时刻必须带 tzinfo")
        if value.utcoffset() != timedelta(0):
            raise ValueError("时刻必须以 UTC 表示")


class ZoneInfoLocalTimeMap:
    """基于标准库 :class:`zoneinfo.ZoneInfo` 的真实时区适配器。

    利用 PEP 495 的 ``fold`` 字段处理歧义：同一挂钟时间分别以
    ``fold=0`` / ``fold=1`` 转成 UTC，相同则为普通时刻，不同则为
    回拨歧义。前拨跳过的时刻两种 fold 都会落到换表后的偏移上，需要
    再用“转 UTC 后转回本地挂钟时间是否保持不变”识别。
    """

    def __init__(self, key: str) -> None:
        if ZoneInfo is None:  # pragma: no cover
            raise RuntimeError("当前 Python 不支持 zoneinfo")
        self.key = key
        self._zone = ZoneInfo(key)

    def resolve(self, local_wall_time: datetime) -> tuple[datetime, ...]:
        if local_wall_time.tzinfo is not None:
            raise ValueError("local_wall_time 必须是不带 tzinfo 的挂钟时间")

        first = local_wall_time.replace(tzinfo=self._zone, fold=0)
        second = local_wall_time.replace(tzinfo=self._zone, fold=1)
        first_utc = first.astimezone(timezone.utc)
        second_utc = second.astimezone(timezone.utc)

        # 前拨跳过：UTC 时刻转回本地后挂钟时间对不上，说明该本地时刻
        # 根本不存在。
        def _exists(utc_instant: datetime) -> bool:
            back = utc_instant.astimezone(self._zone).replace(tzinfo=None)
            return back == local_wall_time

        instants: list[datetime] = []
        for instant in (first_utc, second_utc):
            if _exists(instant) and instant not in instants:
                instants.append(instant)
        return tuple(sorted(instants))

    def to_local(self, utc_instant: datetime) -> datetime:
        if utc_instant.tzinfo is None:
            raise ValueError("时刻必须带 tzinfo")
        return utc_instant.astimezone(self._zone).replace(tzinfo=None)
