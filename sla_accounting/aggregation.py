"""多观察者局部区间合并与跨区域交接去重。

题面要求：

- 区域间交接请求**不能在两边都进独立预算**：同一条逻辑请求
  （同一 request_id）即使被起点区和备用区两个观察者分别上报，
  在服务级结果里也只能出现一次。
- 多个观察者的局部观测区间要汇成服务级结果：对覆盖区间求并集，
  并显式标出观测缺口（没有任何观察者覆盖的时段）。
- 同一观察者对同一请求的完全重复上报视为重传，安全忽略；
  内容冲突的重复上报视为双记账风险，直接拒绝并抛出
  ``DoubleCountingError``，防止污染核算。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .accounting import RequestVerdict, evaluate_request
from .model import LogicalRequest


class DoubleCountingError(ValueError):
    """同一逻辑请求被不同口径重复上报且内容冲突。"""


@dataclass(frozen=True)
class ObservationReport:
    """单个观察者（区域侧探针）的一次局部上报。"""

    observer: str
    region: str
    covered_intervals: tuple[tuple[float, float], ...]
    requests: tuple[LogicalRequest, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.observer:
            raise ValueError("observer 不能为空")
        for lo, hi in self.covered_intervals:
            if lo > hi:
                raise ValueError(f"观察者 {self.observer} 存在反向区间 [{lo}, {hi}]")


def union_intervals(
    intervals: list[tuple[float, float]] | tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    """把若干闭区间合并为互不重叠、按起点排序的并集。

    端点相接（前一区间 hi == 后一区间 lo）视为连续，合并为一个区间。
    """
    ordered = sorted(intervals, key=lambda iv: (iv[0], iv[1]))
    merged: list[list[float]] = []
    for lo, hi in ordered:
        if not merged or lo > merged[-1][1]:
            merged.append([lo, hi])
        else:
            merged[-1][1] = max(merged[-1][1], hi)
    return tuple((lo, hi) for lo, hi in merged)


def coverage_gaps(
    covered: tuple[tuple[float, float], ...],
    horizon: tuple[float, float] | None = None,
) -> tuple[tuple[float, float], ...]:
    """计算观测缺口。

    ``covered`` 必须是已经合并的并集。若给出 ``horizon``（核算全域），
    则返回该域内未被任何观察者覆盖的区间，包括首尾缺口。
    """
    if horizon is None:
        # 没有全域口径时，只能报告相邻覆盖区间之间的内部缺口。
        gaps: list[tuple[float, float]] = []
        for (_, prev_hi), (next_lo, _) in zip(covered, covered[1:]):
            if next_lo > prev_hi:
                gaps.append((prev_hi, next_lo))
        return tuple(gaps)

    h_lo, h_hi = horizon
    gaps = []
    cursor = h_lo
    for lo, hi in covered:
        if hi < h_lo or lo > h_hi:
            continue
        lo = max(lo, h_lo)
        hi = min(hi, h_hi)
        if lo > cursor:
            gaps.append((cursor, lo))
        cursor = max(cursor, hi)
    if cursor < h_hi:
        gaps.append((cursor, h_hi))
    return tuple(gaps)


@dataclass(frozen=True)
class ServiceAggregate:
    """服务级汇总：去重后的请求、核算结论与观测覆盖。"""

    service: str
    requests: tuple[LogicalRequest, ...]
    verdicts: tuple[RequestVerdict, ...]
    coverage: tuple[tuple[float, float], ...]
    gaps: tuple[tuple[float, float], ...]
    duplicate_reports_ignored: int
    observers: frozenset[str]
    regions: frozenset[str]

    def verdict_of(self, request_id: str) -> RequestVerdict:
        for v in self.verdicts:
            if v.request_id == request_id:
                return v
        raise KeyError(f"聚合结果中不存在请求 {request_id}")


def _request_signature(request: LogicalRequest) -> tuple:
    """用于判断两次上报是否为同一内容的规范化签名。

    时钟读数、结果序列、区域、观察者、交接标记全部一致才视为重传。
    """
    return (
        request.request_id,
        request.service,
        request.origin_region,
        request.handed_off,
        tuple(
            (
                a.region,
                a.observer,
                a.start.reading,
                a.start.clock.name,
                a.finish.reading,
                a.finish.clock.name,
                a.outcome,
                a.deadline.reading,
                a.deadline.clock.name,
            )
            for a in request.attempts
        ),
    )


def merge_reports(
    service: str,
    reports: list[ObservationReport] | tuple[ObservationReport, ...],
    horizon: tuple[float, float] | None = None,
) -> ServiceAggregate:
    """把多个观察者的局部上报汇成服务级结果。

    去重规则：
    - 同一 request_id、内容完全一致 -> 重传，忽略并计数；
    - 同一 request_id、内容冲突 -> ``DoubleCountingError``；
    - 不同 request_id -> 各自保留。跨区交接的两侧观测共用 id，
      因此天然只会进入服务级预算一次。
    """
    by_id: dict[str, LogicalRequest] = {}
    signatures: dict[str, tuple] = {}
    duplicates = 0
    intervals: list[tuple[float, float]] = []
    observers: set[str] = set()
    regions: set[str] = set()

    for report in reports:
        observers.add(report.observer)
        regions.add(report.region)
        intervals.extend(report.covered_intervals)
        for request in report.requests:
            if request.service != service:
                raise ValueError(
                    f"观察者 {report.observer} 上报了其他服务 "
                    f"{request.service} 的请求 {request.request_id}"
                )
            signature = _request_signature(request)
            if request.request_id in by_id:
                if signatures[request.request_id] == signature:
                    duplicates += 1
                    continue
                raise DoubleCountingError(
                    f"请求 {request.request_id} 被观察者 "
                    f"{sorted(observers)} 以冲突内容重复上报，"
                    "存在跨区双记账风险，已拒绝"
                )
            by_id[request.request_id] = request
            signatures[request.request_id] = signature

    requests = tuple(
        sorted(by_id.values(), key=lambda r: r.request_id)
    )
    verdicts = tuple(evaluate_request(r) for r in requests)
    coverage = union_intervals(intervals)
    gaps = coverage_gaps(coverage, horizon)

    return ServiceAggregate(
        service=service,
        requests=requests,
        verdicts=verdicts,
        coverage=coverage,
        gaps=gaps,
        duplicate_reports_ignored=duplicates,
        observers=frozenset(observers),
        regions=frozenset(regions),
    )
