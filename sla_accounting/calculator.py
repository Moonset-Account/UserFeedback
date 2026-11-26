"""离线错误预算核算器门面。

把时钟校对、单请求核算、跨区去重、观察者区间合并与滑动窗口扫描
串成一次确定性的离线计算，输出服务级结果。
"""

from __future__ import annotations

from dataclasses import dataclass

from .aggregation import ObservationReport, ServiceAggregate, merge_reports
from .model import Contract
from .windows import (
    WindowOutcome,
    WindowSummary,
    summarize,
    sweep_windows,
)


@dataclass(frozen=True)
class ServiceResult:
    """一个服务的完整离线核算结果。"""

    contract: Contract
    aggregate: ServiceAggregate
    window_timeseries: dict[str, list[WindowOutcome]]
    window_summaries: dict[str, WindowSummary]

    def outcome_at(self, window_name: str, evaluated_at: float) -> WindowOutcome:
        for outcome in self.window_timeseries[window_name]:
            if outcome.evaluated_at == evaluated_at:
                return outcome
        raise KeyError(f"窗口 {window_name} 在 {evaluated_at} 无评估结果")


class OfflineBudgetCalculator:
    """离线核算器：无网络、无副作用，相同输入必得相同输出。"""

    def __init__(self, contract: Contract, horizon: tuple[float, float]):
        self.contract = contract
        self.horizon = horizon

    def evaluate(
        self,
        reports: list[ObservationReport] | tuple[ObservationReport, ...],
        eval_times: tuple[float, ...],
    ) -> ServiceResult:
        aggregate = merge_reports(
            self.contract.service, reports, horizon=self.horizon
        )
        timeseries = sweep_windows(
            self.contract.windows,
            aggregate.verdicts,
            aggregate.requests,
            eval_times,
        )
        summaries = {
            spec.name: summarize(timeseries[spec.name], spec)
            for spec in self.contract.windows
        }
        return ServiceResult(
            contract=self.contract,
            aggregate=aggregate,
            window_timeseries=timeseries,
            window_summaries=summaries,
        )


def build_day_sweep(
    horizon: tuple[float, float], tick_seconds: float
) -> tuple[float, ...]:
    """构造一天内的滑动窗口评估时刻序列。

    从 ``horizon[0] + tick_seconds`` 起，每隔 ``tick_seconds`` 取一个
    评估点，直到并包含 horizon 右端点（最后一个 tick 不足时按右端点补齐）。
    """
    start, end = horizon
    if end <= start:
        raise ValueError("horizon 右端点必须大于左端点")
    if tick_seconds <= 0:
        raise ValueError("tick_seconds 必须为正")

    times: list[float] = []
    t = start + tick_seconds
    while t < end:
        times.append(t)
        t += tick_seconds
    if not times or times[-1] != end:
        times.append(end)
    return tuple(times)
