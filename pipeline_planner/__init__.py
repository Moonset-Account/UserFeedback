"""流水线并行训练规划器（本地基础设施组件）。

入口：
    pipeline_planner.planner.plan(stages, num_microbatches) -> Schedule
    pipeline_planner.planner.verify_schedule(stages, num_microbatches, schedule) -> dict
"""

from .planner import (
    Operation,
    Schedule,
    StageConfig,
    plan,
    verify_schedule,
)

__all__ = [
    "StageConfig",
    "Operation",
    "Schedule",
    "plan",
    "verify_schedule",
]
