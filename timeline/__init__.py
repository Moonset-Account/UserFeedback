"""取证多设备时间线组件。

公共入口：
- TimelineEngine：录入设备/记录/重启/锚点/先后关系，求解记录可能时间范围。
- ConflictError / ConflictReport：锚点矛盾时的冲突范围与溯源。
- 数据模型：Device, Record, SharedEventAnchor, AbsoluteAnchor, Ordering,
  TimeRange, SegmentKey。
"""

from .engine import OrderingResult, TimelineEngine
from .model import (
    AbsoluteAnchor,
    ConflictError,
    ConflictReport,
    Device,
    IntervalClash,
    Ordering,
    Record,
    SegmentKey,
    SharedEventAnchor,
    TimeRange,
)
from .solver import ConstraintGraph

__all__ = [
    "TimelineEngine",
    "OrderingResult",
    "Device",
    "Record",
    "SharedEventAnchor",
    "AbsoluteAnchor",
    "Ordering",
    "TimeRange",
    "SegmentKey",
    "ConflictError",
    "ConflictReport",
    "IntervalClash",
    "ConstraintGraph",
]
