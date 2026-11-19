"""pcm_stitch — 取证拼接库。

将若干段按顺序记录的单声道整数 PCM 片段拼接回完整录音。
每次续录会重复上一段末尾的一部分样本（重叠），调用方给出每个
相邻接缝允许的重叠长度范围；两段相接处的样本必须全部相等，
所有锚点（片段内偏移 -> 整段录音绝对位置）必须同时成立。

静音或周期片段可能产生多个看似合适的接缝：只有被全部可行拼法
共同支持的来源映射才标记为“已确认”，其余区段标记为“歧义”。
没有任何合法拼法时保留原段（status == "no_feasible"）。
每段已确认音频都可以通过 source_at() 反查到片段编号与样本偏移。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, Union

__all__ = ["Anchor", "Region", "Seam", "StitchResult", "stitch"]


# ---------------------------------------------------------------------------
# 输入数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Seam:
    """相邻两段之间允许的重叠长度闭区间 [lo, hi]。"""

    lo: int
    hi: int


@dataclass(frozen=True)
class Anchor:
    """锚点：第 segment 段内偏移 offset 的样本，绝对位置必须是 position。"""

    segment: int
    offset: int
    position: int


@dataclass(frozen=True)
class Region:
    """输出时间轴上的一段连续区间。

    confirmed 为 True 时，segment/offset 给出区间起点对应的来源
    （片段编号、片段内样本偏移），区间内映射线性递增。
    confirmed 为 False 表示歧义区段：存在可行拼法在此处给出不同
    来源映射，或只有部分拼法覆盖该位置。
    """

    start: int
    end: int
    confirmed: bool
    segment: Optional[int] = None
    offset: Optional[int] = None

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("Region 要求 end > start")
        if self.confirmed and (self.segment is None or self.offset is None):
            raise ValueError("已确认区段必须给出来源 segment/offset")


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------

class StitchResult:
    """拼接结果。

    status:
        "ok"          —— 至少存在一种合法拼法，regions 给出确认/歧义划分；
        "no_feasible" —— 没有任何合法拼法，segments 保留原始输入片段。
    """

    def __init__(
        self,
        status: str,
        segments: Tuple[Tuple[int, ...], ...],
        solutions: Tuple[Tuple[int, ...], ...],
        regions: Tuple[Region, ...],
        runs_per_solution: Tuple[Tuple[Region, ...], ...],
        total_length: int,
    ) -> None:
        self.status = status
        self.segments = segments
        self.solutions = solutions
        self.regions = regions
        self._runs_per_solution = runs_per_solution
        self.total_length = total_length

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def confirmed_regions(self) -> Tuple[Region, ...]:
        return tuple(r for r in self.regions if r.confirmed)

    @property
    def ambiguous_regions(self) -> Tuple[Region, ...]:
        return tuple(r for r in self.regions if not r.confirmed)

    def source_at(self, position: int) -> Optional[Tuple[int, int]]:
        """反查绝对位置 position 的来源 (片段编号, 片段内偏移)。

        仅当该位置在所有可行拼法中来源映射一致（已确认）时返回；
        歧义位置或越界位置返回 None。
        """
        if position < 0 or position >= self.total_length:
            return None
        for r in self.regions:
            if r.start <= position < r.end:
                if not r.confirmed:
                    return None
                return (r.segment, r.offset + (position - r.start))  # type: ignore[operator]
        return None

    def value_at(self, position: int) -> Optional[int]:
        """绝对位置 position 的样本值。

        已确认位置直接给出样本；歧义位置若所有覆盖它的可行拼法
        样本值一致，仍返回该值，否则返回 None；越界返回 None。
        """
        if position < 0 or position >= self.total_length:
            return None
        src = self.source_at(position)
        if src is not None:
            return self.segments[src[0]][src[1]]
        values = set()
        for runs in self._runs_per_solution:
            run = _run_at(runs, position)
            if run is not None:
                values.add(
                    self.segments[run.segment][run.offset + (position - run.start)]  # type: ignore[operator]
                )
        if len(values) == 1:
            return values.pop()
        return None

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (
            f"StitchResult(status={self.status!r}, solutions={len(self.solutions)}, "
            f"regions={list(self.regions)!r})"
        )


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _validate_segments(segments: Sequence[Sequence[int]]) -> Tuple[Tuple[int, ...], ...]:
    if not segments:
        raise ValueError("至少需要一段 PCM 片段")
    out: List[Tuple[int, ...]] = []
    for idx, seg in enumerate(segments):
        samples = tuple(seg)
        if not samples:
            raise ValueError(f"第 {idx} 段为空")
        for v in samples:
            if not isinstance(v, int):
                raise TypeError(f"第 {idx} 段含有非整数样本: {v!r}")
        out.append(samples)
    return tuple(out)


def _validate_seams(
    seams: Sequence[Union[Seam, Tuple[int, int]]], n_segments: int
) -> Tuple[Seam, ...]:
    expected = n_segments - 1
    if len(seams) != expected:
        raise ValueError(f"需要 {expected} 个接缝，实际给出 {len(seams)} 个")
    out: List[Seam] = []
    for i, s in enumerate(seams):
        seam = s if isinstance(s, Seam) else Seam(lo=s[0], hi=s[1])
        if not (isinstance(seam.lo, int) and isinstance(seam.hi, int)):
            raise TypeError(f"接缝 {i} 的重叠范围必须是整数")
        if seam.lo < 0 or seam.hi < seam.lo:
            raise ValueError(f"接缝 {i} 的重叠范围非法: [{seam.lo}, {seam.hi}]")
        out.append(seam)
    return tuple(out)


def _validate_anchors(
    anchors: Sequence[Anchor], segs: Tuple[Tuple[int, ...], ...]
) -> Tuple[Anchor, ...]:
    out = []
    for a in anchors:
        if not 0 <= a.segment < len(segs):
            raise ValueError(f"锚点引用了不存在的片段: {a.segment}")
        if not 0 <= a.offset < len(segs[a.segment]):
            raise ValueError(f"锚点偏移越界: 片段 {a.segment} 偏移 {a.offset}")
        if a.position < 0:
            raise ValueError(f"锚点绝对位置不能为负: {a.position}")
        out.append(a)
    return tuple(out)


# ---------------------------------------------------------------------------
# 核心求解
# ---------------------------------------------------------------------------

def _valid_overlaps(prev: Tuple[int, ...], nxt: Tuple[int, ...], lo: int, hi: int) -> List[int]:
    """枚举满足样本全等约束的重叠长度。范围上界按两段长度截断。"""
    hi = min(hi, len(prev), len(nxt))
    out = []
    for k in range(max(lo, 0), hi + 1):
        if k == 0 or prev[-k:] == nxt[:k]:
            out.append(k)
    return out


def _search(
    segs: Tuple[Tuple[int, ...], ...],
    valid: List[List[int]],
    anchors: Tuple[Anchor, ...],
) -> List[Tuple[int, ...]]:
    """回溯枚举所有满足接缝与锚点约束的重叠组合。"""
    n = len(segs)
    by_segment: dict[int, List[Anchor]] = {}
    for a in anchors:
        by_segment.setdefault(a.segment, []).append(a)

    # 首段从零开始，其锚点立即可判定。
    if any(a.position != a.offset for a in by_segment.get(0, ())):
        return []

    starts = [0] * n
    chosen = [0] * (n - 1)
    solutions: List[Tuple[int, ...]] = []

    def bt(i: int) -> None:
        if i == n - 1:
            solutions.append(tuple(chosen))
            return
        for k in valid[i]:
            nxt_start = starts[i] + len(segs[i]) - k
            if all(
                nxt_start + a.offset == a.position
                for a in by_segment.get(i + 1, ())
            ):
                starts[i + 1] = nxt_start
                chosen[i] = k
                bt(i + 1)

    bt(0)
    return solutions


def _runs_for(
    segs: Tuple[Tuple[int, ...], ...], overlaps: Tuple[int, ...]
) -> Tuple[Tuple[Region, ...], int]:
    """单一拼法的来源映射。重叠区样本相等，归属较早的片段。"""
    runs: List[Region] = [Region(0, len(segs[0]), True, 0, 0)]
    end = len(segs[0])
    for i, k in enumerate(overlaps):
        seg = i + 1
        new_end = end + len(segs[seg]) - k
        if new_end > end:
            runs.append(Region(end, new_end, True, seg, k))
        end = new_end
    return tuple(runs), end


def _intersect_runs(a_runs: Tuple[Region, ...], b_runs: Tuple[Region, ...]) -> Tuple[Region, ...]:
    """两组来源映射的一致部分：同一绝对区间且来源映射逐点相同。"""
    out: List[Region] = []
    i = j = 0
    while i < len(a_runs) and j < len(b_runs):
        a, b = a_runs[i], b_runs[j]
        lo, hi = max(a.start, b.start), min(a.end, b.end)
        if (
            lo < hi
            and a.segment == b.segment
            and (a.offset - a.start) == (b.offset - b.start)  # type: ignore[operator]
        ):
            out.append(Region(lo, hi, True, a.segment, a.offset + (lo - a.start)))  # type: ignore[operator]
        if a.end < b.end:
            i += 1
        elif b.end < a.end:
            j += 1
        else:
            i += 1
            j += 1
    return tuple(out)


def _with_ambiguous(confirmed: Tuple[Region, ...], total: int) -> Tuple[Region, ...]:
    """在 [0, total) 内补上歧义区段，形成完整时间轴。"""
    regions: List[Region] = []
    pos = 0
    for r in confirmed:
        if r.start > pos:
            regions.append(Region(pos, r.start, False))
        regions.append(r)
        pos = r.end
    if pos < total:
        regions.append(Region(pos, total, False))
    return tuple(regions)


def _run_at(runs: Tuple[Region, ...], position: int) -> Optional[Region]:
    for r in runs:
        if r.start <= position < r.end:
            return r
    return None


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def stitch(
    segments: Sequence[Sequence[int]],
    seams: Sequence[Union[Seam, Tuple[int, int]]] = (),
    anchors: Sequence[Anchor] = (),
) -> StitchResult:
    """拼接若干段单声道整数 PCM。

    参数:
        segments: 按记录顺序排列的整数样本片段，首段绝对位置从零开始。
        seams:    每个相邻接缝允许的重叠长度范围 (lo, hi) 或 Seam。
        anchors:  片段内偏移 -> 绝对位置的锚点，须全部同时成立。

    返回 StitchResult；无合法拼法时 status 为 "no_feasible"，
    result.segments 保留原始片段。
    """
    segs = _validate_segments(segments)
    seam_list = _validate_seams(seams, len(segs))
    anchor_list = _validate_anchors(anchors, segs)

    valid = [
        _valid_overlaps(segs[i], segs[i + 1], seam.lo, seam.hi)
        for i, seam in enumerate(seam_list)
    ]
    solutions = _search(segs, valid, anchor_list)

    if not solutions:
        return StitchResult(
            status="no_feasible",
            segments=segs,
            solutions=(),
            regions=(),
            runs_per_solution=(),
            total_length=0,
        )

    runs_per_solution = []
    total = 0
    for overlaps in solutions:
        runs, end = _runs_for(segs, overlaps)
        runs_per_solution.append(runs)
        total = max(total, end)

    confirmed = runs_per_solution[0]
    for runs in runs_per_solution[1:]:
        confirmed = _intersect_runs(confirmed, runs)
        if not confirmed:
            break

    regions = _with_ambiguous(confirmed, total)
    return StitchResult(
        status="ok",
        segments=segs,
        solutions=tuple(solutions),
        regions=regions,
        runs_per_solution=tuple(runs_per_solution),
        total_length=total,
    )
