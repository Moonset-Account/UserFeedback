"""半开时间区间 [start, end) 的纯函数工具。

所有区间都用 ``(start, end)`` 二元组表示，遵循半开约定：
包含 start，不包含 end。判定函数只依赖比较运算，因此 int、date 等
可比较类型都可以作为端点。
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

Interval = Tuple[object, object]


def validate_interval(start: object, end: object) -> None:
    """校验区间端点，非法时抛出 :class:`ValueError`。"""
    if start is None or end is None:
        raise ValueError("interval endpoints must not be None")
    if not start < end:
        raise ValueError(
            f"half-open interval requires start < end, got [{start!r}, {end!r})"
        )


def merge_intervals(intervals: Sequence[Interval]) -> List[Interval]:
    """把若干区间合并为不相交、有序且已并集（含相邻粘合）的区间列表。

    相邻区间 ``[a, b)`` 与 ``[b, c)`` 会被合并成 ``[a, c)``，因为按半开
    约定它们之间不存在空隙。
    """
    ordered = sorted(intervals, key=lambda item: item[0])
    merged: List[Interval] = []
    for start, end in ordered:
        if not merged:
            merged.append((start, end))
            continue
        last_start, last_end = merged[-1]
        if start <= last_end:
            # 相交或恰好相邻（start == last_end）都合并
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def uncovered_parts(target: Interval, cover: Sequence[Interval]) -> List[Interval]:
    """返回 ``target`` 中没有被 ``cover`` 并集覆盖到的子区间。

    ``cover`` 可以是任意顺序、互相重叠的区间；返回值为有序、不相交的
    半开区间列表。完全覆盖时返回空列表。
    """
    target_start, target_end = target
    missing: List[Interval] = []
    cursor = target_start
    for cover_start, cover_end in merge_intervals(cover):
        if cover_end <= cursor:
            # 覆盖段整体落在游标之前，无贡献
            continue
        if cover_start >= target_end:
            # 覆盖段已经越过目标区间末端
            break
        if cover_start > cursor:
            # 游标到下一段覆盖起点之间是一段缺口
            missing.append((cursor, min(cover_start, target_end)))
        cursor = max(cursor, cover_end)
        if cursor >= target_end:
            break
    if cursor < target_end:
        missing.append((cursor, target_end))
    return missing
