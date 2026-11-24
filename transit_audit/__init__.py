"""传输合规核查库（transit_audit）。

公开 API：

- 数据模型：:class:`ByteRange`、:class:`NodeKind`、:class:`RouteNode`、
  :class:`HopReceipt`、:class:`DeliveryResult`、:class:`Attempt`、
  :class:`Segment`、:class:`Batch`
- 核查入口：:func:`verify_batch`、:func:`verify_segment`
- 报告与结论：:class:`BatchReport`、:class:`SegmentReport`、
  :class:`Violation`、:class:`ViolationKind`、:class:`SegmentStatus`、
  :class:`ArchivedAttempt`
- 区间工具：:func:`merge_ranges`、:func:`intersect_ranges`、
  :func:`uncovered_ranges`、:func:`excess_ranges`
"""

from __future__ import annotations

from .audit import (
    ArchivedAttempt,
    BatchReport,
    SegmentReport,
    SegmentStatus,
    Violation,
    ViolationKind,
    verify_batch,
    verify_segment,
)
from .intervals import excess_ranges, intersect_ranges, merge_ranges, uncovered_ranges
from .model import (
    Attempt,
    Batch,
    ByteRange,
    DeliveryResult,
    HopReceipt,
    NodeKind,
    RouteNode,
    Segment,
)

__all__ = [
    # 模型
    "ByteRange",
    "NodeKind",
    "RouteNode",
    "HopReceipt",
    "DeliveryResult",
    "Attempt",
    "Segment",
    "Batch",
    # 核查
    "verify_batch",
    "verify_segment",
    # 报告
    "BatchReport",
    "SegmentReport",
    "SegmentStatus",
    "Violation",
    "ViolationKind",
    "ArchivedAttempt",
    # 区间工具
    "merge_ranges",
    "intersect_ranges",
    "uncovered_ranges",
    "excess_ranges",
]
