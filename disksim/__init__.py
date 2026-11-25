"""带旋转延迟的磁盘读调度仿真器。

公开 API 见 :mod:`disksim.model` 与 :mod:`disksim.scheduler`。
"""

from .model import DiskConfig, Request, ServiceRecord, SimulationResult, TimelineEvent
from .scheduler import DiskScheduler, evaluate_order, plan_service

__all__ = [
    "DiskConfig",
    "Request",
    "ServiceRecord",
    "SimulationResult",
    "TimelineEvent",
    "DiskScheduler",
    "plan_service",
    "evaluate_order",
]
