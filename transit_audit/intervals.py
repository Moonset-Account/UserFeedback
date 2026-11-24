"""半开整数字节区间 [start, end) 的基础运算。

所有函数均为纯函数，输入输出都是 :class:`~transit_audit.model.ByteRange`
或由其组成的元组。区间一律采用左闭右开语义，因此首尾相接的两个区间
``[0, 10)`` 与 ``[10, 20)`` 视为连续，中间不存在缺口。
"""

from __future__ import annotations

from .model import ByteRange


def merge_ranges(ranges: list[ByteRange] | tuple[ByteRange, ...]) -> tuple[ByteRange, ...]:
    """合并相互重叠或首尾相接的区间，返回按起点排序的不相交区间。

    >>> merge_ranges([ByteRange(5, 10), ByteRange(0, 5), ByteRange(8, 12)])
    (ByteRange(start=0, end=12),)
    """
    if not ranges:
        return ()
    ordered = sorted(ranges, key=lambda item: (item.start, item.end))
    merged: list[ByteRange] = [ordered[0]]
    for current in ordered[1:]:
        last = merged[-1]
        if current.start <= last.end:
            if current.end > last.end:
                merged[-1] = ByteRange(last.start, current.end)
        else:
            merged.append(current)
    return tuple(merged)


def intersect_ranges(
    ranges: list[ByteRange] | tuple[ByteRange, ...], bound: ByteRange
) -> tuple[ByteRange, ...]:
    """取一组区间与 ``bound`` 的交集，并合并结果。

    完全落在边界外的区间会被丢弃；部分越界的区间会被截断到边界内。
    """
    pieces: list[ByteRange] = []
    for item in ranges:
        start = max(item.start, bound.start)
        end = min(item.end, bound.end)
        if start < end:
            pieces.append(ByteRange(start, end))
    return merge_ranges(pieces)


def uncovered_ranges(
    covered: list[ByteRange] | tuple[ByteRange, ...], target: ByteRange
) -> tuple[ByteRange, ...]:
    """返回 ``target`` 内未被 ``covered`` 覆盖的子区间（缺口）。"""
    clipped = intersect_ranges(covered, target)
    gaps: list[ByteRange] = []
    cursor = target.start
    for piece in clipped:
        if piece.start > cursor:
            gaps.append(ByteRange(cursor, piece.start))
        cursor = max(cursor, piece.end)
    if cursor < target.end:
        gaps.append(ByteRange(cursor, target.end))
    return tuple(gaps)


def excess_ranges(
    ranges: list[ByteRange] | tuple[ByteRange, ...], bound: ByteRange
) -> tuple[ByteRange, ...]:
    """返回 ``ranges`` 中落在 ``bound`` 之外的部分（越界子区间）。"""
    outside: list[ByteRange] = []
    for item in ranges:
        if item.start < bound.start:
            outside.append(ByteRange(item.start, min(item.end, bound.start)))
        if item.end > bound.end:
            outside.append(ByteRange(max(item.start, bound.end), item.end))
    return merge_ranges(outside)
