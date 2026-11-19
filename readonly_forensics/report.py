"""恢复报告结构：每个区段都可回查到物理范围。"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Mapping

from .model import (
    ACQUISITION_ERROR,
    ACQUISITION_MISSING,
    ACQUISITION_SHORT,
    MAPPING_MISSING,
    OVERWRITTEN,
    REALLOCATED,
    RECOVERED,
    RECORD_GAP,
    RecoveryCase,
)

_REJECTION_REASONS = frozenset(
    {
        REALLOCATED,
        RECORD_GAP,
        OVERWRITTEN,
        ACQUISITION_MISSING,
        ACQUISITION_ERROR,
        ACQUISITION_SHORT,
        MAPPING_MISSING,
    }
)

_REASON_TEXT = {
    RECOVERED: "归属连续且采集成功",
    REALLOCATED: "删除后该范围曾分配给其他对象，原归属断裂",
    RECORD_GAP: "删除后存在分配/释放记录缺口，归属不可追溯",
    OVERWRITTEN: "删除后该范围被改写，现存字节不是原字节",
    ACQUISITION_MISSING: "缺少对应物理簇的采集分块",
    ACQUISITION_ERROR: "采集分块报告错误，未取得可用字节",
    ACQUISITION_SHORT: "采集短读，可用字节不足以覆盖该范围",
    MAPPING_MISSING: "逻辑偏移缺少到物理簇的映射",
}


@dataclass(frozen=True)
class Segment:
    """文件逻辑维度上的一个判定区段。

    物理回查坐标：
      - 有映射时给出 physical_cluster 与 cluster_offset_start/end；
      - 无映射（mapping_missing）时物理坐标为 None，原因中说明。
    """

    index: int
    file_offset_start: int
    file_offset_end: int
    logical_cluster: int
    status: str
    reason: str
    detail: str
    data: bytes | None
    physical_cluster: int | None
    cluster_offset_start: int | None
    cluster_offset_end: int | None
    evidence: tuple[str, ...]

    @property
    def length(self) -> int:
        return self.file_offset_end - self.file_offset_start

    @property
    def is_recovered(self) -> bool:
        return self.status == RECOVERED

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "file_offset_start": self.file_offset_start,
            "file_offset_end": self.file_offset_end,
            "length": self.length,
            "logical_cluster": self.logical_cluster,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "physical_cluster": self.physical_cluster,
            "cluster_offset_start": self.cluster_offset_start,
            "cluster_offset_end": self.cluster_offset_end,
            "evidence": list(self.evidence),
            "data_b64": (
                base64.b64encode(self.data).decode("ascii")
                if self.data is not None
                else None
            ),
        }


@dataclass(frozen=True)
class RecoveryReport:
    """整文件恢复结果：区段列表、汇总与原输入参数回显。"""

    file_size: int
    cluster_size: int
    file_name: str | None
    segments: tuple[Segment, ...]
    recovered_bytes: int
    missing_bytes: int
    fully_recovered: bool
    reason_counts: Mapping[str, int]

    def to_dict(self) -> dict:
        return {
            "file_name": self.file_name,
            "file_size": self.file_size,
            "cluster_size": self.cluster_size,
            "recovered_bytes": self.recovered_bytes,
            "missing_bytes": self.missing_bytes,
            "fully_recovered": self.fully_recovered,
            "reason_counts": dict(self.reason_counts),
            "segments": [seg.to_dict() for seg in self.segments],
        }

    def materialize(self) -> bytes:
        """拼接所有已恢复区段；被拒绝区段在结果中缺失（不做填充猜测）。"""
        return b"".join(seg.data or b"" for seg in self.segments if seg.is_recovered)

    def render_text(self) -> str:
        """生成便于人工阅读的文本报告。"""
        lines = [
            f"取证恢复报告: {self.file_name or '<未命名文件>'}",
            f"文件大小: {self.file_size}  簇大小: {self.cluster_size}",
            f"已恢复: {self.recovered_bytes} 字节 / "
            f"缺失: {self.missing_bytes} 字节 / "
            f"完整恢复: {'是' if self.fully_recovered else '否'}",
            f"原因统计: {dict(self.reason_counts)}",
            "-" * 72,
        ]
        for seg in self.segments:
            if seg.physical_cluster is None:
                loc = "物理位置: 无映射"
            else:
                loc = (
                    f"物理位置: 簇 {seg.physical_cluster}"
                    f" 内字节 [{seg.cluster_offset_start}, {seg.cluster_offset_end})"
                )
            lines.append(
                f"#{seg.index} 文件[{seg.file_offset_start}, {seg.file_offset_end})"
                f" 逻辑簇 {seg.logical_cluster} [{seg.status}/{seg.reason}]"
            )
            lines.append(f"    {loc}")
            lines.append(f"    说明: {seg.detail}")
            if seg.evidence:
                lines.append(f"    证据: {', '.join(seg.evidence)}")
            if seg.is_recovered:
                lines.append(
                    f"    数据({len(seg.data)}字节): "
                    f"{base64.b64encode(seg.data).decode('ascii')}"
                )
        return "\n".join(lines)


def build_report(case: RecoveryCase, segments: list[Segment]) -> RecoveryReport:
    """根据判定完成的区段列表组装汇总报告。"""
    reason_counts: dict[str, int] = {}
    recovered = 0
    covered = 0
    for seg in segments:
        if seg.status not in _REJECTION_REASONS and seg.status != RECOVERED:
            raise ValueError(f"未知区段状态: {seg.status}")
        reason_counts[seg.reason] = reason_counts.get(seg.reason, 0) + seg.length
        covered += seg.length
        if seg.is_recovered:
            if seg.data is None or len(seg.data) != seg.length:
                raise ValueError("recovered 区段的数据长度必须与区段长度一致")
            recovered += seg.length

    if covered != case.file_size:
        raise ValueError(
            f"区段覆盖 {covered} 字节与文件大小 {case.file_size} 不一致"
        )

    return RecoveryReport(
        file_size=case.file_size,
        cluster_size=case.cluster_size,
        file_name=case.file_name,
        segments=tuple(segments),
        recovered_bytes=recovered,
        missing_bytes=case.file_size - recovered,
        fully_recovered=recovered == case.file_size,
        reason_counts=dict(sorted(reason_counts.items())),
    )


def reason_text(reason: str) -> str:
    return _REASON_TEXT[reason]
