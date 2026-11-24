"""合规核查核心：按最终收货尝试把各跳回执接成连续获准路线。

核查规则（题面）：

1. 每个分段只取最后收货成功（``DELIVERED``）的那次传输尝试进行路线核查；
   若存在多次成功尝试，取尝试编号最大者。失败尝试以及被取代的较早成功
   尝试单独留档（``archived_attempts``）。
2. 沿该次尝试的预期路线逐跳核对：每个节点回执覆盖的字节区间，合并后
   必须完整覆盖该分段的预期字节范围，且不得越出该范围。
   - 回执区间允许乱序、重叠、首尾相接（``[0,10)`` + ``[10,20)`` 视为连续）；
   - 同跳存在覆盖缺口 -> ``MISSING_COVERAGE``，给出缺口子区间与节点；
   - 回执越过分段边界 -> ``OUT_OF_BOUNDS``，给出越界子区间与节点；
   - 路线上的节点没有任何回执 -> ``MISSING_HOP``，缺的整跳按整段区间报告；
   - 出现路线之外的节点回执 -> ``UNKNOWN_NODE``。
3. 没有任何成功收货尝试的分段判为 ``UNDELIVERED``，不进行路线核查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .intervals import excess_ranges, merge_ranges, uncovered_ranges
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


class ViolationKind(str, Enum):
    """核查发现的违规类型。"""

    OUT_OF_BOUNDS = "out_of_bounds"
    MISSING_COVERAGE = "missing_coverage"
    MISSING_HOP = "missing_hop"
    UNKNOWN_NODE = "unknown_node"


class SegmentStatus(str, Enum):
    """单个分段的核查结论。"""

    COMPLIANT = "compliant"
    NON_COMPLIANT = "non_compliant"
    UNDELIVERED = "undelivered"


_KIND_LABELS = {
    NodeKind.RELAY: "中继",
    NodeKind.REGION: "区域",
}


@dataclass(frozen=True)
class Violation:
    """一条具体违规记录，定位到节点与字节子区间。"""

    kind: ViolationKind
    node: str
    node_kind: NodeKind
    ranges: tuple[ByteRange, ...]
    message: str

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "node": self.node,
            "node_kind": self.node_kind.value,
            "ranges": [{"start": item.start, "end": item.end} for item in self.ranges],
            "message": self.message,
        }


@dataclass(frozen=True)
class ArchivedAttempt:
    """留档的非核查尝试：失败尝试或被更大编号成功尝试取代的较早尝试。"""

    attempt_no: int
    result: DeliveryResult
    reason: str

    def to_dict(self) -> dict:
        return {
            "attempt_no": self.attempt_no,
            "result": self.result.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SegmentReport:
    """单个分段的核查报告。"""

    segment_id: str
    status: SegmentStatus
    expected_range: ByteRange
    selected_attempt_no: int | None
    violations: tuple[Violation, ...] = field(default_factory=tuple)
    archived_attempts: tuple[ArchivedAttempt, ...] = field(default_factory=tuple)

    @property
    def is_compliant(self) -> bool:
        return self.status is SegmentStatus.COMPLIANT

    def to_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "status": self.status.value,
            "expected_range": {"start": self.expected_range.start, "end": self.expected_range.end},
            "selected_attempt_no": self.selected_attempt_no,
            "violations": [item.to_dict() for item in self.violations],
            "archived_attempts": [item.to_dict() for item in self.archived_attempts],
        }


@dataclass(frozen=True)
class BatchReport:
    """整批数据的核查报告。"""

    segment_reports: tuple[SegmentReport, ...]

    @property
    def compliant(self) -> bool:
        """所有分段均合规时为真；存在未送达或违规分段时为假。"""
        return all(report.is_compliant for report in self.segment_reports)

    @property
    def violations(self) -> tuple[Violation, ...]:
        """跨全部分段的违规明细（按分段顺序、跳号顺序排列）。"""
        return tuple(
            violation
            for report in self.segment_reports
            for violation in report.violations
        )

    def to_dict(self) -> dict:
        return {
            "compliant": self.compliant,
            "segment_reports": [item.to_dict() for item in self.segment_reports],
        }


def _select_attempt(attempts: tuple[Attempt, ...]) -> tuple[Attempt | None, tuple[ArchivedAttempt, ...]]:
    """选出用于核查的尝试，并把其余尝试留档。

    选择规则：在所有最终收货成功的尝试中取编号最大者；没有成功尝试时
    返回 ``None``。失败尝试与被取代的较早成功尝试分别标注原因后留档。
    """
    delivered = sorted(
        (item for item in attempts if item.result is DeliveryResult.DELIVERED),
        key=lambda item: item.attempt_no,
    )
    selected = delivered[-1] if delivered else None
    archived: list[ArchivedAttempt] = []
    for item in sorted(attempts, key=lambda entry: entry.attempt_no):
        if selected is not None and item.attempt_no == selected.attempt_no:
            continue
        if item.result is DeliveryResult.FAILED:
            reason = "收货失败，单独留档，不参与获准路线核查"
        else:
            reason = (
                f"较早的成功收货尝试，被编号更大的尝试 {selected.attempt_no if selected else '?'} "
                "取代，留档备查"
            )
        archived.append(ArchivedAttempt(item.attempt_no, item.result, reason))
    return selected, tuple(archived)


def _audit_attempt(segment: Segment, attempt: Attempt) -> tuple[Violation, ...]:
    """对选中的单次成功尝试逐跳核查，返回有序违规列表。"""
    expected = segment.expected_range
    receipts_by_node: dict[str, HopReceipt] = {item.node: item for item in attempt.receipts}
    violations: list[Violation] = []
    ordered_nodes: list[RouteNode] = sorted(attempt.route, key=lambda item: item.sequence)

    for hop in ordered_nodes:
        label = _KIND_LABELS[hop.kind]
        receipt = receipts_by_node.pop(hop.node, None)
        if receipt is None:
            # 整条路线缺少该跳回执：缺的整跳按分段完整区间报告。
            violations.append(
                Violation(
                    kind=ViolationKind.MISSING_HOP,
                    node=hop.node,
                    node_kind=hop.kind,
                    ranges=(expected,),
                    message=(
                        f"{label}节点 {hop.node}（第 {hop.sequence} 跳）缺少交接回执，"
                        f"分段 {segment.segment_id} 的字节区间 "
                        f"[{expected.start}, {expected.end}) 在该跳无任何覆盖"
                    ),
                )
            )
            continue

        merged = merge_ranges(receipt.ranges)

        # 1) 越界：回执实际覆盖落在分段预期范围之外的部分。
        outside = excess_ranges(merged, expected)
        if outside:
            formatted = ", ".join(f"[{item.start}, {item.end})" for item in outside)
            violations.append(
                Violation(
                    kind=ViolationKind.OUT_OF_BOUNDS,
                    node=hop.node,
                    node_kind=hop.kind,
                    ranges=outside,
                    message=(
                        f"{label}节点 {hop.node}（第 {hop.sequence} 跳）的回执越出分段 "
                        f"{segment.segment_id} 的预期范围 [{expected.start}, {expected.end})，"
                        f"越界子区间：{formatted}"
                    ),
                )
            )

        # 2) 缺口：该跳回执合并后未覆盖到的分段内子区间。
        gaps = uncovered_ranges(merged, expected)
        if gaps:
            formatted = ", ".join(f"[{item.start}, {item.end})" for item in gaps)
            violations.append(
                Violation(
                    kind=ViolationKind.MISSING_COVERAGE,
                    node=hop.node,
                    node_kind=hop.kind,
                    ranges=gaps,
                    message=(
                        f"{label}节点 {hop.node}（第 {hop.sequence} 跳）的回执未能连续覆盖分段 "
                        f"{segment.segment_id} 的预期范围 [{expected.start}, {expected.end})，"
                        f"缺口子区间：{formatted}"
                    ),
                )
            )

    # 3) 路线之外的节点回执（无法对应到任何预期跳）。
    for node, receipt in sorted(receipts_by_node.items()):
        merged = merge_ranges(receipt.ranges)
        formatted = ", ".join(f"[{item.start}, {item.end})" for item in merged)
        label = _KIND_LABELS[receipt.kind]
        violations.append(
            Violation(
                kind=ViolationKind.UNKNOWN_NODE,
                node=node,
                node_kind=receipt.kind,
                ranges=merged,
                message=(
                    f"出现预期路线之外的{label}节点 {node} 的回执，"
                    f"分段 {segment.segment_id} 未授权该跳，回执区间：{formatted}"
                ),
            )
        )

    return tuple(violations)


def verify_segment(
    segment: Segment, attempts: list[Attempt] | tuple[Attempt, ...]
) -> SegmentReport:
    """核查单个分段。``attempts`` 为该分段的全部传输尝试（含失败尝试）。"""
    if not isinstance(segment, Segment):
        raise ValueError(f"segment 必须是 Segment，实际为 {segment!r}")
    if not isinstance(attempts, (list, tuple)):
        raise ValueError("attempts 必须是 Attempt 列表/元组")
    attempt_tuple = tuple(attempts)
    for item in attempt_tuple:
        if not isinstance(item, Attempt):
            raise ValueError(f"attempts 中的元素必须是 Attempt，实际为 {item!r}")

    selected, archived = _select_attempt(attempt_tuple)
    if selected is None:
        return SegmentReport(
            segment_id=segment.segment_id,
            status=SegmentStatus.UNDELIVERED,
            expected_range=segment.expected_range,
            selected_attempt_no=None,
            violations=(),
            archived_attempts=archived,
        )

    violations = _audit_attempt(segment, selected)
    return SegmentReport(
        segment_id=segment.segment_id,
        status=SegmentStatus.NON_COMPLIANT if violations else SegmentStatus.COMPLIANT,
        expected_range=segment.expected_range,
        selected_attempt_no=selected.attempt_no,
        violations=violations,
        archived_attempts=archived,
    )


def verify_batch(batch: Batch) -> BatchReport:
    """核查整批数据，按 ``batch.segments`` 的顺序产出每段报告。"""
    if not isinstance(batch, Batch):
        raise ValueError(f"batch 必须是 Batch，实际为 {batch!r}")
    reports: list[SegmentReport] = []
    for segment in batch.segments:
        reports.append(verify_segment(segment, batch.attempts.get(segment.segment_id, ())))
    return BatchReport(tuple(reports))
