"""半开时间区间 [start, end) 的基础运算。

所有时间统一使用时区感知的 UTC ``datetime``。模块只依赖标准库。
区间采用左闭右开语义，因此相邻区间 ``[a, b)`` 与 ``[b, c)`` 之间没有空隙，
合并后为 ``[a, c)``，时长不会重复计算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

_DURATION_RE = re.compile(
    r"^P"
    r"(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?"
    r"$"
)


def parse_dt(value: datetime | str) -> datetime:
    """把 ISO-8601 字符串（含 ``Z`` 后缀）或 datetime 规范为 UTC datetime。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise TypeError(
            f"期望 ISO-8601 字符串或 datetime，实际为 {type(value).__name__}"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    """序列化为 ``...Z`` 形式的 UTC ISO-8601 字符串。"""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_duration(value: timedelta | int | float | str) -> timedelta:
    """解析额度时长。

    支持：``timedelta``、数字（秒）、ISO-8601 时长字符串（如 ``PT1H30M``、``P2D``）。
    """
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool):
        raise TypeError("布尔值不能作为时长")
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    if isinstance(value, str):
        match = _DURATION_RE.match(value.strip())
        if not match or not any(match.groups()):
            raise ValueError(f"无法解析的 ISO-8601 时长: {value!r}")
        days, hours, minutes, seconds = match.groups()
        return timedelta(
            days=int(days or 0),
            hours=int(hours or 0),
            minutes=int(minutes or 0),
            seconds=float(seconds or 0),
        )
    raise TypeError(f"无法解析的时长类型: {type(value).__name__}")


def format_duration(td: timedelta) -> str:
    """把 timedelta 格式化为 ISO-8601 时长字符串。"""
    total = int(td.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    text = "P"
    if days:
        text += f"{days}D"
    if hours or minutes or seconds or not days:
        text += "T"
        if hours:
            text += f"{hours}H"
        if minutes:
            text += f"{minutes}M"
        if seconds or (not hours and not minutes):
            text += f"{seconds}S"
    return text


@dataclass(frozen=True)
class Interval:
    """不可变的半开区间 [start, end)。"""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"区间结束时间 {self.end} 早于开始时间 {self.start}")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {"start": to_iso(self.start), "end": to_iso(self.end)}

    @classmethod
    def from_dict(cls, data: dict | list | tuple) -> "Interval":
        if isinstance(data, (list, tuple)):
            if len(data) != 2:
                raise ValueError("数组形式的区间必须恰好包含 start、end 两个元素")
            start, end = data
        elif isinstance(data, dict):
            start, end = data["start"], data["end"]
        else:
            raise TypeError(f"无法解析的区间形式: {type(data).__name__}")
        return cls(parse_dt(start), parse_dt(end))


def clip(interval: Interval, window: Interval) -> Optional[Interval]:
    """把区间裁剪到考核窗口内；完全落在窗口外时返回 ``None``。"""
    start = max(interval.start, window.start)
    end = min(interval.end, window.end)
    if start >= end:
        return None
    return Interval(start, end)


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """对区间做并集合并：重叠或相邻的区间合并为一个，丢弃零长度区间。"""
    ordered = sorted(
        (iv for iv in intervals if iv.start < iv.end),
        key=lambda iv: (iv.start, iv.end),
    )
    merged: list[Interval] = []
    for iv in ordered:
        if not merged or iv.start > merged[-1].end:
            merged.append(iv)
        elif iv.end > merged[-1].end:
            merged.append(Interval(merged[-1].start, iv.end))
            merged.pop(-2)
    return merged


def union_duration(intervals: Iterable[Interval]) -> timedelta:
    """合并后求总时长——重叠部分只计算一次。"""
    return sum((iv.duration for iv in merge(intervals)), timedelta(0))


def intersect(a_intervals: Iterable[Interval], b_intervals: Iterable[Interval]):
    """求两组已排序区间的交集。"""
    left = merge(a_intervals)
    right = merge(b_intervals)
    out: list[Interval] = []
    i = j = 0
    while i < len(left) and j < len(right):
        start = max(left[i].start, right[j].start)
        end = min(left[i].end, right[j].end)
        if start < end:
            out.append(Interval(start, end))
        if left[i].end <= right[j].end:
            i += 1
        else:
            j += 1
    return out


def tail_after_budget(
    intervals: Iterable[Interval],
    used: timedelta,
    budget: timedelta,
) -> list[Interval]:
    """按时间顺序跳过已消耗的时长后，返回超出额度的时间段。

    ``intervals`` 应为合并后的有序区间；``used`` 是当期已用额度（对应这些区间
    靠前的部分），``budget`` 是总额度。返回值即“导致超量的时间段”。
    """
    remaining_budget = budget - used
    if remaining_budget <= timedelta(0):
        return list(intervals)
    overage: list[Interval] = []
    for iv in intervals:
        if remaining_budget >= iv.duration:
            remaining_budget -= iv.duration
        else:
            overage.append(Interval(iv.start + remaining_budget, iv.end))
            remaining_budget = timedelta(0)
    return overage
