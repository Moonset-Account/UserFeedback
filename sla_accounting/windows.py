"""滑动窗口错误预算核算。

对每个承诺的滑动窗口，输出三类边界：

- ``definite_violations``：窗口内**确定违约**的事件数（时钟误差下
  仍然成立的违约）；
- ``possible_violations``：窗口内**可能违约**的事件数（受时钟误差
  影响，违约与否无法确定）；
- ``remaining_best`` / ``remaining_worst``：剩余额度的最好/最坏边界。

额度语义（B = 窗口预算，D = 确定违约数，P = 可能违约数）：

- 最好情况：所有可能违约最终都被证明守约，消耗只有 D，
  剩余 = B - D；
- 最坏情况：所有可能违约都被证明违约，消耗 D + P，
  剩余 = B - D - P（可能为负，表示即使预算耗尽仍有超额违约）。

窗口状态：

- ``OK``：最坏情况下预算仍有余量（D + P < B）；
- ``AT_RISK``：确定消耗未超预算，但最坏情况会超（D <= B < D + P）；
- ``EXHAUSTED``：确定违约已耗尽或超出预算（D >= B）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .accounting import RequestVerdict, ViolationStatus
from .clocks import interval_overlaps
from .model import LogicalRequest, WindowSpec



class WindowStatus(str):
    OK = "ok"
    AT_RISK = "at_risk"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class WindowOutcome:
    """某一评估时刻、某个滑动窗口的核算结果。"""

    window_name: str
    evaluated_at: float
    window_start: float
    window_end: float
    budget: int
    definite_violations: int
    possible_violations: int
    request_ids: tuple[str, ...]
    remaining_best: int
    remaining_worst: int
    status: WindowStatus


@dataclass(frozen=True)
class WindowSummary:
    """一天扫描中某个窗口的聚合摘要。"""

    window_name: str
    length_seconds: float
    budget: int
    ever_exhausted: bool
    ever_at_risk: bool
    max_definite: int
    max_possible: int
    min_remaining_best: int
    min_remaining_worst: int
    first_exhausted_at: float | None


def _request_event_interval(
    verdict: RequestVerdict, requests_by_id: dict[str, LogicalRequest]
) -> tuple[float, float]:
    request = requests_by_id[verdict.request_id]
    lo = min(a.finish.lo for a in request.attempts)
    hi = max(a.finish.hi for a in request.attempts)
    return (lo, hi)


def evaluate_window(
    spec: WindowSpec,
    evaluated_at: float,
    verdicts: tuple[RequestVerdict, ...],
    requests_by_id: dict[str, LogicalRequest],
) -> WindowOutcome:
    """评估 ``evaluated_at`` 时刻回看一个滑动窗口的预算消耗。

    窗口为闭区间 ``[evaluated_at - length, evaluated_at]``。事件的
    参考时间区间与窗口相交即纳入（时钟误差下事件归属窗口本身也可能
    有歧义，纳入交集是保守口径，不确定性已体现在 possible 计数中）。
    """
    start = evaluated_at - spec.length_seconds
    window = (start, evaluated_at)

    inside: list[RequestVerdict] = []
    for verdict in verdicts:
        event_interval = _request_event_interval(verdict, requests_by_id)
        if interval_overlaps(event_interval, window):
            inside.append(verdict)

    definite = sum(
        1 for v in inside if v.status == ViolationStatus.DEFINITE
    )
    possible = sum(
        1 for v in inside if v.status == ViolationStatus.POSSIBLE
    )
    remaining_best = spec.error_budget - definite
    remaining_worst = spec.error_budget - definite - possible

    if definite >= spec.error_budget:
        status = WindowStatus.EXHAUSTED
    elif definite + possible >= spec.error_budget:
        status = WindowStatus.AT_RISK
    else:
        status = WindowStatus.OK

    return WindowOutcome(
        window_name=spec.name,
        evaluated_at=evaluated_at,
        window_start=start,
        window_end=evaluated_at,
        budget=spec.error_budget,
        definite_violations=definite,
        possible_violations=possible,
        request_ids=tuple(v.request_id for v in inside),
        remaining_best=remaining_best,
        remaining_worst=remaining_worst,
        status=status,
    )


def sweep_windows(
    windows: tuple[WindowSpec, ...],
    verdicts: tuple[RequestVerdict, ...],
    requests: tuple[LogicalRequest, ...],
    eval_times: tuple[float, ...],
) -> dict[str, list[WindowOutcome]]:
    """在一组评估时刻上对全部滑动窗口做扫描，返回窗口名 -> 时序结果。"""
    requests_by_id = {r.request_id: r for r in requests}
    results: dict[str, list[WindowOutcome]] = {w.name: [] for w in windows}
    for at in eval_times:
        for spec in windows:
            results[spec.name].append(
                evaluate_window(spec, at, verdicts, requests_by_id)
            )
    return results


def summarize(outcomes: list[WindowOutcome], spec: WindowSpec) -> WindowSummary:
    """把一个窗口的全天扫描结果压缩成摘要。"""
    first_exhausted = next(
        (o.evaluated_at for o in outcomes if o.status == WindowStatus.EXHAUSTED),
        None,
    )
    return WindowSummary(
        window_name=spec.name,
        length_seconds=spec.length_seconds,
        budget=spec.error_budget,
        ever_exhausted=any(o.status == WindowStatus.EXHAUSTED for o in outcomes),
        ever_at_risk=any(o.status == WindowStatus.AT_RISK for o in outcomes),
        max_definite=max((o.definite_violations for o in outcomes), default=0),
        max_possible=max((o.possible_violations for o in outcomes), default=0),
        min_remaining_best=min((o.remaining_best for o in outcomes), default=spec.error_budget),
        min_remaining_worst=min(
            (o.remaining_worst for o in outcomes), default=spec.error_budget
        ),
        first_exhausted_at=first_exhausted,
    )
