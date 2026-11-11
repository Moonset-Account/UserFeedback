"""证据不足原因码与共享数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# 崩溃线程中找不到任何应用帧（is_app_frame=true），缺少归并所需的关键帧
MISSING_KEY_FRAME = "MISSING_KEY_FRAME"
# 构建映射缺失：构建标识未知 / 模块不匹配 / 地址查无记录 / 候选符号无映射支撑
MISSING_MAPPING = "MISSING_MAPPING"
# 多义映射：同一地址经映射可解析到多个互不一致的稳定源码归属
AMBIGUOUS_MAPPING = "AMBIGUOUS_MAPPING"
# 报告自身结构不完整，缺少必备字段
INVALID_REPORT = "INVALID_REPORT"

INSUFFICIENT_REASONS = (
    MISSING_KEY_FRAME,
    MISSING_MAPPING,
    AMBIGUOUS_MAPPING,
    INVALID_REPORT,
)


@dataclass(frozen=True)
class StableAttribution:
    """跨构建稳定的源码归属。

    取首个应用帧解析链的最内层帧源码位置 + 非内联归属函数（inline_depth=0）。
    原始地址会随构建变化、内联层次可能增减，但该三元组保持稳定。
    """

    source_file: str
    owning_symbol: str
    source_line: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "owning_symbol": self.owning_symbol,
            "source_line": self.source_line,
        }

    def key(self) -> tuple[str, str, int]:
        return (self.source_file, self.owning_symbol, self.source_line)


@dataclass
class MemberEvidence:
    """单份报告归入某组的可复核证据。"""

    report_id: str
    module_build_id: str
    source_revision: str
    frame_index: int
    module: str
    raw_address: str
    candidate_symbols: list[str]
    max_inline_depth: int
    resolved_chain: list[dict[str, Any]]
    stable_attribution: StableAttribution
    report_corroborated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "module_build_id": self.module_build_id,
            "source_revision": self.source_revision,
            "frame_index": self.frame_index,
            "module": self.module,
            "raw_address": self.raw_address,
            "candidate_symbols": list(self.candidate_symbols),
            "max_inline_depth": self.max_inline_depth,
            "resolved_chain": self.resolved_chain,
            "stable_attribution": self.stable_attribution.as_dict(),
            "report_corroborated": self.report_corroborated,
        }


@dataclass
class PendingRecord:
    """证据不足、保留待核查的报告。"""

    report_id: str
    reason_code: str
    reason_detail: str
    module_build_id: str | None = None
    fault_category: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "module_build_id": self.module_build_id,
            "fault_category": self.fault_category,
            "reason_code": self.reason_code,
            "reason_detail": self.reason_detail,
        }


@dataclass
class MergeGroup:
    fault_category: str
    attribution: StableAttribution
    members: list[MemberEvidence] = field(default_factory=list)

    @property
    def group_key(self) -> tuple[str, str, str, int]:
        return (self.fault_category,) + self.attribution.key()
