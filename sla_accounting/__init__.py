"""跨区域切流错误预算离线核算器。

模块划分：
- clocks:      可校对时钟源与观测时间区间，确定/可能/未违约三态判定
- model:       逻辑请求、重试跳、SLA 合同与滑动窗口口径
- accounting:  单条逻辑请求的违约判定（首跳豁免、超期重试不可消除）
- aggregation: 跨区交接去重、多观察者局部区间合并
- windows:     滑动窗口预算核算与剩余额度边界
- calculator:  离线核算器门面，输出服务级结果
"""

from .clocks import ClockSource, ObservedTime, Ternary, compare, meets_deadline
from .model import Attempt, Contract, LogicalRequest, Outcome, WindowSpec
from .accounting import RequestVerdict, evaluate_request
from .aggregation import (
    DoubleCountingError,
    ObservationReport,
    ServiceAggregate,
    merge_reports,
    union_intervals,
)
from .windows import (
    WindowOutcome,
    WindowStatus,
    WindowSummary,
    sweep_windows,
    evaluate_window,
)
from .calculator import OfflineBudgetCalculator, ServiceResult, build_day_sweep

__all__ = [
    "ClockSource",
    "ObservedTime",
    "Ternary",
    "compare",
    "meets_deadline",
    "Attempt",
    "Contract",
    "LogicalRequest",
    "Outcome",
    "WindowSpec",
    "RequestVerdict",
    "evaluate_request",
    "DoubleCountingError",
    "ObservationReport",
    "ServiceAggregate",
    "merge_reports",
    "union_intervals",
    "WindowOutcome",
    "WindowStatus",
    "WindowSummary",
    "sweep_windows",
    "evaluate_window",
    "OfflineBudgetCalculator",
    "ServiceResult",
    "build_day_sweep",
]
