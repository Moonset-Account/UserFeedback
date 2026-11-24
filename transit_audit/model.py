"""传输合规核查库的输入数据模型与字段校验。

字节区间统一使用左闭右开的半开区间 :class:`ByteRange`，即
``ByteRange(start=0, end=100)`` 覆盖字节 0..99。所有模型都是不可变
（``frozen=True``）的值对象，构造时即完成字段合法性校验，非法输入
抛出 :class:`ValueError`（类型错误仍抛出原生 :class:`TypeError`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


def _require_non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} 必须是非负整数，实际为 {value!r}")
    if value < 0:
        raise ValueError(f"{field} 不能为负数，实际为 {value}")
    return value


def _require_positive_int(value: object, field: str) -> int:
    parsed = _require_non_negative_int(value, field)
    if parsed == 0:
        raise ValueError(f"{field} 必须是正整数，实际为 0")
    return parsed


@dataclass(frozen=True)
class ByteRange:
    """半开字节区间 ``[start, end)``，``start`` 必须小于 ``end``。"""

    start: int
    end: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.start, "ByteRange.start")
        _require_non_negative_int(self.end, "ByteRange.end")
        if self.end <= self.start:
            raise ValueError(
                f"ByteRange 必须满足 start < end，实际为 start={self.start}, end={self.end}"
            )


class NodeKind(str, Enum):
    """交接节点的角色：中继节点或区域（接收端）节点。"""

    RELAY = "relay"
    REGION = "region"


@dataclass(frozen=True)
class RouteNode:
    """预期获准路线上的一跳。

    ``sequence`` 为从 1 开始的跳号，``kind`` 标明该跳是中继还是区域回执。
    """

    node: str
    sequence: int
    kind: NodeKind

    def __post_init__(self) -> None:
        if not isinstance(self.node, str) or not self.node.strip():
            raise ValueError(f"RouteNode.node 必须是非空字符串，实际为 {self.node!r}")
        _require_positive_int(self.sequence, "RouteNode.sequence")
        if not isinstance(self.kind, NodeKind):
            raise ValueError(f"RouteNode.kind 必须是 NodeKind，实际为 {self.kind!r}")


@dataclass(frozen=True)
class HopReceipt:
    """某次传输尝试中，一个节点确认的交接回执。

    中继可把一段数据拆成几次传送，因此一份回执可携带多个实际覆盖的
    字节区间，区间允许乱序、重叠或首尾相接。
    """

    node: str
    kind: NodeKind
    ranges: tuple[ByteRange, ...]

    def __init__(self, node: str, kind: NodeKind, ranges: list[ByteRange] | tuple[ByteRange, ...]) -> None:
        # frozen dataclass 上自定义 __init__ 以完成集合类型归一化与校验。
        if not isinstance(node, str) or not node.strip():
            raise ValueError(f"HopReceipt.node 必须是非空字符串，实际为 {node!r}")
        if not isinstance(kind, NodeKind):
            raise ValueError(f"HopReceipt.kind 必须是 NodeKind，实际为 {kind!r}")
        if not isinstance(ranges, (list, tuple)) or not ranges:
            raise ValueError("HopReceipt.ranges 必须是非空的 ByteRange 列表/元组")
        normalized = tuple(ranges)
        for item in normalized:
            if not isinstance(item, ByteRange):
                raise ValueError(f"HopReceipt.ranges 中的元素必须是 ByteRange，实际为 {item!r}")
        object.__setattr__(self, "node", node)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "ranges", normalized)


class DeliveryResult(str, Enum):
    """单次传输尝试的最后收货结果。"""

    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass(frozen=True)
class Attempt:
    """一个数据段的一次传输尝试。

    ``attempt_no`` 为尝试编号（正整数，同一数据段内唯一）；
    ``route`` 是该次尝试预期的获准路线，按跳号升序给出；
    ``receipts`` 是沿途实际收到的中继与区域回执；
    ``result`` 是最终收货结果。
    """

    attempt_no: int
    route: tuple[RouteNode, ...]
    receipts: tuple[HopReceipt, ...]
    result: DeliveryResult

    def __init__(
        self,
        attempt_no: int,
        route: list[RouteNode] | tuple[RouteNode, ...],
        receipts: list[HopReceipt] | tuple[HopReceipt, ...],
        result: DeliveryResult,
    ) -> None:
        _require_positive_int(attempt_no, "Attempt.attempt_no")
        if not isinstance(route, (list, tuple)) or not route:
            raise ValueError("Attempt.route 必须是非空的 RouteNode 列表/元组")
        route_tuple = tuple(route)
        for item in route_tuple:
            if not isinstance(item, RouteNode):
                raise ValueError(f"Attempt.route 中的元素必须是 RouteNode，实际为 {item!r}")
        sequences = [item.sequence for item in route_tuple]
        if sorted(sequences) != list(range(1, len(route_tuple) + 1)):
            raise ValueError(
                "Attempt.route 的跳号 sequence 必须恰好覆盖 1..N 且不重复，"
                f"实际为 {sequences}"
            )
        if not isinstance(receipts, (list, tuple)):
            raise ValueError("Attempt.receipts 必须是 HopReceipt 列表/元组")
        receipt_tuple = tuple(receipts)
        for item in receipt_tuple:
            if not isinstance(item, HopReceipt):
                raise ValueError(f"Attempt.receipts 中的元素必须是 HopReceipt，实际为 {item!r}")
        receipt_nodes = [item.node for item in receipt_tuple]
        if len(receipt_nodes) != len(set(receipt_nodes)):
            raise ValueError(f"Attempt.receipts 中同一节点只能出现一份回执，实际为 {receipt_nodes}")
        if not isinstance(result, DeliveryResult):
            raise ValueError(f"Attempt.result 必须是 DeliveryResult，实际为 {result!r}")
        object.__setattr__(self, "attempt_no", attempt_no)
        object.__setattr__(self, "route", route_tuple)
        object.__setattr__(self, "receipts", receipt_tuple)
        object.__setattr__(self, "result", result)


@dataclass(frozen=True)
class Segment:
    """一个预期数据分段：编号与其应当覆盖的字节范围。"""

    segment_id: str
    expected_range: ByteRange

    def __post_init__(self) -> None:
        if not isinstance(self.segment_id, str) or not self.segment_id.strip():
            raise ValueError(f"Segment.segment_id 必须是非空字符串，实际为 {self.segment_id!r}")
        if not isinstance(self.expected_range, ByteRange):
            raise ValueError(
                f"Segment.expected_range 必须是 ByteRange，实际为 {self.expected_range!r}"
            )


@dataclass(frozen=True)
class Batch:
    """一次核查的完整输入：预期分段集合与每段的传输尝试。"""

    segments: tuple[Segment, ...]
    attempts: dict[str, tuple[Attempt, ...]]

    def __init__(
        self,
        segments: list[Segment] | tuple[Segment, ...],
        attempts: dict[str, list[Attempt] | tuple[Attempt, ...]],
    ) -> None:
        if not isinstance(segments, (list, tuple)) or not segments:
            raise ValueError("Batch.segments 必须是非空的 Segment 列表/元组")
        segment_tuple = tuple(segments)
        for item in segment_tuple:
            if not isinstance(item, Segment):
                raise ValueError(f"Batch.segments 中的元素必须是 Segment，实际为 {item!r}")
        segment_ids = [item.segment_id for item in segment_tuple]
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError(f"Batch.segments 的 segment_id 不能重复，实际为 {segment_ids}")
        if not isinstance(attempts, dict):
            raise ValueError("Batch.attempts 必须是 dict[segment_id, Attempt 序列]")
        normalized: dict[str, tuple[Attempt, ...]] = {}
        known = set(segment_ids)
        for key, value in attempts.items():
            if not isinstance(key, str) or key not in known:
                raise ValueError(
                    f"Batch.attempts 的键必须是已知的 segment_id，发现 {key!r}"
                )
            if not isinstance(value, (list, tuple)):
                raise ValueError(f"分段 {key} 的尝试必须是 Attempt 列表/元组")
            attempt_tuple = tuple(value)
            for item in attempt_tuple:
                if not isinstance(item, Attempt):
                    raise ValueError(f"分段 {key} 的尝试元素必须是 Attempt，实际为 {item!r}")
            numbers = [item.attempt_no for item in attempt_tuple]
            if len(numbers) != len(set(numbers)):
                raise ValueError(f"分段 {key} 的 attempt_no 不能重复，实际为 {numbers}")
            normalized[key] = attempt_tuple
        object.__setattr__(self, "segments", segment_tuple)
        object.__setattr__(self, "attempts", normalized)
