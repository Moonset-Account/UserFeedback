"""半开区间 [start, end) 覆盖图工具。

纯函数实现，便于独立单元测试。区间集合始终保持“有序、互不重叠、互不相邻”的
归一化形态；加入新区间时自动合并重叠与相邻部分。
"""

from __future__ import annotations

from typing import List, Tuple

Interval = Tuple[int, int]


def merge(spans: List[Interval]) -> List[Interval]:
    """将任意区间集合归一化为有序、不重叠、不相邻的区间集合。"""
    if not spans:
        return []
    ordered = sorted((s, e) for s, e in spans if e > s)
    out: List[Interval] = []
    for start, end in ordered:
        if out and start <= out[-1][1]:
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def add(spans: List[Interval], start: int, end: int) -> List[Interval]:
    """把 [start, end) 加入覆盖图并返回新的归一化区间集合。"""
    if end <= start:
        return list(spans)
    return merge([*spans, (start, end)])


def gaps(spans: List[Interval], length: int) -> List[Interval]:
    """返回覆盖图在 [0, length) 内的全部缺口。"""
    missing: List[Interval] = []
    cursor = 0
    for start, end in merge(spans):
        if start > cursor:
            missing.append((cursor, min(start, length)))
        cursor = max(cursor, min(end, length))
        if cursor >= length:
            break
    if cursor < length:
        missing.append((cursor, length))
    return missing


def first_gap(spans: List[Interval], length: int) -> Interval | None:
    """返回最靠前的缺口；覆盖完整时返回 None。"""
    missing = gaps(spans, length)
    return missing[0] if missing else None


def covered_count(spans: List[Interval], length: int) -> int:
    """统计 [0, length) 内已覆盖的字节数。"""
    return sum(min(e, length) - s for s, e in merge(spans) if s < length)
