"""一帧开关排程模块（最多 6 入 6 出端口）。

公开 API：
    Problem, BeatRecord, Schedule, MissedDemand, IllegalPlanError, InfeasibleError
    solve, solve_safe, replay, replan, enumerate_matchings, lower_bounds
"""

from .model import (
    Problem,
    BeatRecord,
    Schedule,
    MissedDemand,
    InfeasibleError,
    IllegalPlanError,
)
from .scheduler import solve, solve_safe, enumerate_matchings, lower_bounds
from .replay import replay, replan

__all__ = [
    "Problem",
    "BeatRecord",
    "Schedule",
    "MissedDemand",
    "InfeasibleError",
    "IllegalPlanError",
    "solve",
    "solve_safe",
    "replay",
    "replan",
    "enumerate_matchings",
    "lower_bounds",
]
