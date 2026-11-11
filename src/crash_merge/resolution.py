"""单份报告的证据解析。

定位崩溃线程中可用的首个相关应用帧（is_app_frame=true 的第一帧），
通过构建映射把易变的原始地址解析为跨构建稳定的源码归属，并与报告自带的
候选符号、内联层次交叉验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .mapping import BuildMappingIndex, attribution_of, normalize_address, serialize_chain
from .models import (
    AMBIGUOUS_MAPPING,
    MISSING_KEY_FRAME,
    MISSING_MAPPING,
    MemberEvidence,
    StableAttribution,
)


@dataclass
class Resolution:
    """一份报告的解析结果：成功得到证据，或得到待核查原因。"""

    report_id: str
    module_build_id: str | None
    fault_category: str | None
    evidence: MemberEvidence | None = None
    pending_reason: str | None = None
    pending_detail: str | None = None


def _candidate_symbol_set(frame: dict[str, Any]) -> set[str]:
    raw = frame.get("candidate_symbols") or []
    return {s for s in raw if isinstance(s, str) and s}


def _report_innermost(frame: dict[str, Any]) -> dict[str, Any] | None:
    """报告自带内联层次中的最内层帧（inline_depth 最大）。"""
    hierarchy = frame.get("inline_hierarchy")
    if not isinstance(hierarchy, list) or not hierarchy:
        return None
    valid = [
        f
        for f in hierarchy
        if isinstance(f, dict)
        and isinstance(f.get("inline_depth"), int)
        and isinstance(f.get("symbol"), str)
    ]
    if not valid:
        return None
    return max(valid, key=lambda f: f["inline_depth"])


def _entry_supported_by_candidates(
    entry: dict[str, Any], candidate_symbols: set[str]
) -> bool:
    """映射条目的链上符号是否与报告候选符号有交集（无候选时不做否定判断）。"""
    if not candidate_symbols:
        return True
    mapped_symbols = {f["symbol"] for f in entry["inline_chain"]}
    return bool(mapped_symbols & candidate_symbols)


def _chains_corroborate(
    entry: dict[str, Any], frame: dict[str, Any]
) -> bool:
    """报告自带内联层次是否与映射链互相印证。

    要求：最内层符号一致、最大内联深度一致。报告缺失层次信息时视为无法印证
    （而非否定），由调用方据此置 report_corroborated=false。
    """
    report_inner = _report_innermost(frame)
    if report_inner is None:
        return False
    mapped_inner = max(entry["inline_chain"], key=lambda f: f["inline_depth"])
    mapped_depth = max(f["inline_depth"] for f in entry["inline_chain"])
    return (
        report_inner["symbol"] == mapped_inner["symbol"]
        and report_inner["inline_depth"] == mapped_depth
    )


def resolve_report(
    report: dict[str, Any], index: BuildMappingIndex
) -> Resolution:
    report_id = report.get("report_id")
    build_id = report.get("module_build_id")
    fault = report.get("fault_category")

    def pending(reason: str, detail: str) -> Resolution:
        return Resolution(
            report_id=report_id,
            module_build_id=build_id,
            fault_category=fault,
            pending_reason=reason,
            pending_detail=detail,
        )

    crash_thread = report.get("crash_thread")
    frames = crash_thread.get("frames") if isinstance(crash_thread, dict) else None
    if not isinstance(frames, list) or not frames:
        return pending(MISSING_KEY_FRAME, "报告缺少崩溃线程帧，无法定位应用帧")

    app_frames = [
        (i, f)
        for i, f in enumerate(frames)
        if isinstance(f, dict) and f.get("is_app_frame") is True
    ]
    if not app_frames:
        return pending(MISSING_KEY_FRAME, "崩溃线程中不存在应用帧（is_app_frame=true）")

    frame_index, key_frame = app_frames[0]  # 首个相关应用帧
    module = key_frame.get("module")
    raw_address = normalize_address(key_frame.get("address"))

    if not isinstance(build_id, str) or not build_id:
        return pending(MISSING_MAPPING, "报告缺少 module_build_id，无法选择构建映射")
    if not index.is_known_build(build_id):
        return pending(MISSING_MAPPING, f"构建标识 {build_id} 在映射中无记录")
    if not isinstance(module, str) or not module:
        return pending(MISSING_MAPPING, "首个应用帧缺少 module 字段")
    if raw_address is None:
        return pending(MISSING_MAPPING, "首个应用帧地址非法或缺失")

    entries = index.lookup_entries(build_id, module, raw_address)
    if entries is None:
        return pending(
            MISSING_MAPPING,
            f"构建 {build_id} 模块 {module} 地址 {raw_address} 无映射条目",
        )

    candidate_symbols = _candidate_symbol_set(key_frame)
    supported = [
        entry
        for entry in entries
        if _entry_supported_by_candidates(entry, candidate_symbols)
    ]
    if not supported:
        return pending(
            MISSING_MAPPING,
            "映射条目符号与报告候选符号无交集，候选符号缺少映射支撑: "
            + ", ".join(sorted(candidate_symbols)),
        )

    # 多义判定：受支撑条目若解析出多个互不一致的稳定归属，则证据不足
    attributions = {attribution_of(entry).key(): attribution_of(entry)
                    for entry in supported}
    if len(attributions) > 1:
        distinct = sorted(
            f"{a.source_file}:{a.source_line}@{a.owning_symbol}"
            for a in attributions.values()
        )
        return pending(
            AMBIGUOUS_MAPPING,
            f"同一地址存在 {len(attributions)} 个互不一致的稳定归属: "
            + " | ".join(distinct),
        )

    entry = supported[0]
    attribution: StableAttribution = attributions[next(iter(attributions))]
    corroborated = _chains_corroborate(entry, key_frame)
    max_depth = max(f["inline_depth"] for f in entry["inline_chain"])

    evidence = MemberEvidence(
        report_id=report_id,
        module_build_id=build_id,
        source_revision=index.source_revision(build_id) or "",
        frame_index=frame_index,
        module=module,
        raw_address=raw_address,
        candidate_symbols=sorted(candidate_symbols),
        max_inline_depth=max_depth,
        resolved_chain=serialize_chain(entry),
        stable_attribution=attribution,
        report_corroborated=corroborated,
    )
    return Resolution(
        report_id=report_id,
        module_build_id=build_id,
        fault_category=fault,
        evidence=evidence,
    )
