"""半开区间 [start, end) 的区间集合代数。

`end=None` 表示 +∞，用于缓冲区长度为动态值时“清零整个缓冲区”
所覆盖的范围。写入只覆盖实际区间，不做对齐放大。
"""
from __future__ import annotations

Interval = tuple  # (start: int, end: int | None)


def _end_key(end):
    # None（+∞）排在任何有限端点之后
    return (1, 0) if end is None else (0, end)


def normalize(intervals):
    """排序并合并重叠/相邻区间，返回不相交区间列表。"""
    items = sorted(
        ((s, e) for s, e in intervals if e is None or e > s),
        key=lambda iv: (iv[0], _end_key(iv[1])),
    )
    out = []
    for s, e in items:
        if out:
            ls, le = out[-1]
            if le is None or s <= le:
                if le is None or e is None:
                    out[-1] = (ls, None)
                elif e > le:
                    out[-1] = (ls, e)
                continue
        out.append((s, e))
    return out


def add(intervals, start, end):
    """并入一个新区间（写入的实际区间）。"""
    return normalize([*intervals, (start, end)])


def intersect(a, b):
    """交集：汇合点只保留所有前驱路径都已初始化的区间。"""
    a = normalize(a)
    b = normalize(b)
    out = []
    i = j = 0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        ea, eb = a[i][1], b[j][1]
        if ea is None:
            e = eb
        elif eb is None:
            e = ea
        else:
            e = min(ea, eb)
        if e is None or e > s:
            out.append((s, e))
        if ea is None:
            j += 1
        elif eb is None:
            i += 1
        elif ea <= eb:
            i += 1
        else:
            j += 1
    return out


def subtract(a, b):
    """差集：a 中未被 b 覆盖的部分（用于求复制区间内的未初始化空洞）。"""
    out = []
    for s, e in normalize(a):
        cur = s  # 当前未被覆盖的起点；None 表示整段已被覆盖
        for bs, be in normalize(b):
            if cur is None:
                break
            if be is not None and be <= cur:
                continue
            if e is not None and bs >= e:
                break
            if bs > cur:
                out.append((cur, bs if e is None else min(bs, e)))
            if be is None:
                cur = None
            else:
                cur = be
        if cur is not None and (e is None or cur < e):
            out.append((cur, e))
    return out


def covers(intervals, start, end):
    """判断 [start, end) 是否被区间集合完整覆盖。"""
    for s, e in normalize(intervals):
        if s > start:
            continue
        if e is None:
            return True
        if end is not None and e >= end:
            return True
    return False
