"""日期时间归一化与实例身份工具。

三类时间的语义（RFC 5545 §3.3.5 / §3.3.12）：
- 全日事件：DTSTART 是 DATE（无时间部分），时长按天计；
- 浮动时间：DATE-TIME 不带 TZID 且不是 UTC（无 Z 后缀），解释为"墙上时间"，
  本采集器始终保留 naive datetime，不替源数据擅自附加时区；
- 显式时区：带 TZID 的 DATE-TIME，icalendar 解析后为 aware datetime（含 VTIMEZONE 定义）；
- UTC：以 Z 结尾的 DATE-TIME。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def normalize_timezone(value: dt.datetime) -> dt.datetime:
    """把 aware 时间的 tzinfo 统一换成标准库 zoneinfo。

    icalendar 5 自带的是 pytz 风格 tzinfo，其 DST 偏移绑定在构造时刻，
    dateutil 生成新日期时不会重算（11 月仍可能拿到 EDT 偏移）。
    转换时保留同一 UTC 瞬间，再转回目标墙上时间，保证 DST 偏移始终正确。
    """
    if value.tzinfo is None:
        return value
    tzname = str(value.tzinfo)
    try:
        target_tz = ZoneInfo(tzname)
    except ZoneInfoNotFoundError:
        return value  # 系统时区库缺失时保留原对象（tzdata 包已在依赖中兜底）
    return value.astimezone(dt.timezone.utc).astimezone(target_tz)


@dataclass(frozen=True)
class TimeKind:
    kind: str  # "all_day" | "floating" | "tz_aware"

    @property
    def label(self) -> str:
        return {
            "all_day": "全日事件",
            "floating": "浮动时间（源数据未声明时区）",
            "tz_aware": "显式时区/UTC",
        }[self.kind]


def classify(value: dt.datetime | dt.date) -> str:
    """判断一个 DTSTART/DTEND 值的时间类型。"""
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            return "tz_aware"
        return "floating"
    return "all_day"


def normalize_utc(value: dt.datetime) -> str | None:
    """aware 时间转 UTC ISO 字符串；naive/date 返回 None（不伪造时区）。"""
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        return value.astimezone(dt.timezone.utc).isoformat()
    return None


def isoformat_local(value: dt.datetime | dt.date) -> str:
    """稳定的本地表示（保留原始墙上时间，不做时区换算）。"""
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None).isoformat()
    return value.isoformat()


def tzid_of(prop) -> str | None:
    """从 icalendar 属性对象上取 TZID 参数。"""
    params = getattr(prop, "params", None)
    if not params:
        return None
    tzid = params.get("TZID")
    if tzid is None:
        return None
    return str(tzid)


def instance_key(value: dt.datetime | dt.date) -> str:
    """实例在事件内部的身份键。

    键的构造保留时间类型与原始墙上时间；显式时区事件的 aware datetime
    还带 UTC 偏移，因此夏令时切换前后不会发生键碰撞。
    """
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            offset = value.utcoffset()
            off_min = int(offset.total_seconds() // 60) if offset is not None else 0
            return f"dt:{value.replace(tzinfo=None).isoformat()}|off:{off_min:+d}"
        return f"float:{value.isoformat()}"
    return f"date:{value.isoformat()}"


def overlaps_range(start: dt.datetime | dt.date,
                   end: dt.datetime | dt.date,
                   range_lo: dt.datetime | dt.date,
                   range_hi: dt.datetime | dt.date) -> bool:
    """半开区间 [start, end) 与 [range_lo, range_hi) 是否相交。"""
    return start < range_hi and end > range_lo
