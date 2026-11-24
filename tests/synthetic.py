"""测试用确定性合成时区。

模拟一个 UTC 偏移在 -5（标准时）/ -4（夏令时）之间切换的时区：

* 春令时：2026-03-08 07:00 UTC 换表，当地 02:00 直接跳到 03:00，
  当地 02:00~02:59:59 不存在；
* 秋令时：2026-11-01 06:00 UTC 换表，当地 02:00 回拨到 01:00，
  当地 01:00~01:59:59 出现两次。
"""

from datetime import datetime, time, timedelta, timezone

from job_controller import SyntheticZone, Transition

UTC = timezone.utc

SYNTHETIC_ZONE = SyntheticZone(
    name="Synthetic/America",
    initial_offset=timedelta(hours=-5),
    transitions=(
        Transition(
            # 当地标准时 01:00 -> 夏令时 02:00，缺口 [01:00, 02:00)，
            # 因此每日 01:30 在这一天不存在
            at_utc=datetime(2026, 3, 8, 6, tzinfo=UTC),
            new_offset=timedelta(hours=-4),
        ),
        Transition(
            # 当地夏令时 02:00 -> 标准时 01:00，01:00~01:59 出现两次
            at_utc=datetime(2026, 11, 1, 6, tzinfo=UTC),
            new_offset=timedelta(hours=-5),
        ),
    ),
)

# 每日作业的计划挂钟时间
LOCAL_01_30 = time(1, 30)

# 关键 UTC 时刻
SPRING_GAP_UTC = datetime(2026, 3, 8, 5, 30, tzinfo=UTC)
"""当地 01:30 处于前拨跳过区间（无对应 UTC）；该常量仅用于 to_local。"""

FALLBACK_FIRST_UTC = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
"""回拨歧义的第一遍 01:30（夏令时 -4）。"""

FALLBACK_SECOND_UTC = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
"""回拨歧义的第二遍 01:30（标准时 -5）。"""

WINTER_INSTANT_UTC = datetime(2026, 6, 2, 5, 30, tzinfo=UTC)
"""普通日期的 01:30（夏令时 -4），唯一映射。"""


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def local(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """naive 当地挂钟时间。"""
    return datetime(year, month, day, hour, minute)
