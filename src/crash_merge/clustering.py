"""分组聚合与可复核归并理由生成。"""

from __future__ import annotations

import hashlib
from typing import Any

from .models import (
    INVALID_REPORT,
    MemberEvidence,
    MergeGroup,
    PendingRecord,
)
from .resolution import Resolution


def _group_id(fault_category: str, attribution_key: tuple[str, str, int]) -> str:
    source_file, owning_symbol, source_line = attribution_key
    digest = hashlib.sha256(
        f"{fault_category}|{source_file}|{owning_symbol}|{source_line}".encode("utf-8")
    ).hexdigest()[:12]
    return f"GRP-{digest}"


def cluster(resolutions: list[Resolution], raw_reports: list[dict[str, Any]]) -> dict[str, Any]:
    """按 (故障类别, 稳定源码归属) 聚合，输出可解释的分组结果。

    同一源码位置的不同故障类别自然落入不同分组；缺少关键帧 / 映射问题 /
    多义映射的报告不参与聚合，统一保留为待核查。
    """
    groups: dict[tuple[str, str, str, int], MergeGroup] = {}
    pending: list[PendingRecord] = []
    seen_report_ids: set[str] = set()

    for resolution in resolutions:
        rid = resolution.report_id
        if rid in seen_report_ids:
            pending.append(
                PendingRecord(
                    report_id=rid,
                    reason_code=INVALID_REPORT,
                    reason_detail="report_id 重复，已跳过后续出现的记录",
                    module_build_id=resolution.module_build_id,
                    fault_category=resolution.fault_category,
                )
            )
            continue
        seen_report_ids.add(rid)

        if resolution.evidence is None:
            pending.append(
                PendingRecord(
                    report_id=rid,
                    reason_code=resolution.pending_reason or INVALID_REPORT,
                    reason_detail=resolution.pending_detail or "未知解析失败",
                    module_build_id=resolution.module_build_id,
                    fault_category=resolution.fault_category,
                )
            )
            continue

        evidence: MemberEvidence = resolution.evidence
        if not isinstance(resolution.fault_category, str) or not resolution.fault_category:
            pending.append(
                PendingRecord(
                    report_id=rid,
                    reason_code=INVALID_REPORT,
                    reason_detail="报告缺少 fault_category，无法形成分组键",
                    module_build_id=resolution.module_build_id,
                    fault_category=resolution.fault_category,
                )
            )
            continue

        group = groups.get(_key_of(resolution))
        if group is None:
            group = MergeGroup(
                fault_category=resolution.fault_category,
                attribution=evidence.stable_attribution,
            )
            groups[group.group_key] = group
        group.members.append(evidence)

    serialized_groups = [
        _serialize_group(group)
        for group in sorted(groups.values(), key=_group_sort_key)
    ]
    pending.sort(key=lambda p: (p.reason_code, p.report_id))

    return {
        "summary": {
            "total_reports": len(raw_reports),
            "grouped_reports": sum(g["member_count"] for g in serialized_groups),
            "group_count": len(serialized_groups),
            "pending_review_count": len(pending),
            "pending_reason_counts": _reason_counts(pending),
        },
        "groups": serialized_groups,
        "pending_review": [p.as_dict() for p in pending],
    }


def _key_of(resolution: Resolution) -> tuple[str, str, str, int]:
    assert resolution.evidence is not None
    attr = resolution.evidence.stable_attribution
    return (resolution.fault_category, attr.source_file, attr.owning_symbol, attr.source_line)


def _group_sort_key(group: MergeGroup) -> tuple[str, str, int, str]:
    attr = group.attribution
    return (attr.source_file, attr.owning_symbol, attr.source_line, group.fault_category)


def _reason_counts(pending: list[PendingRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in pending:
        counts[record.reason_code] = counts.get(record.reason_code, 0) + 1
    return dict(sorted(counts.items()))


def _serialize_group(group: MergeGroup) -> dict[str, Any]:
    members = sorted(group.members, key=lambda m: m.report_id)
    representative = members[0]
    attr = group.attribution
    builds = sorted({m.module_build_id for m in members})
    revisions = sorted({m.source_revision for m in members})
    uncorroborated = [m.report_id for m in members if not m.report_corroborated]

    rationale_lines = [
        f"归并键 = 故障类别({group.fault_category}) + 稳定源码归属"
        f"({attr.source_file}:{attr.source_line} @ {attr.owning_symbol})；"
        f"原始地址与内联帧随构建变化，不参与归并键。",
        f"各成员均取崩溃线程中首个相关应用帧（frame_index 见成员证据），"
        f"经构建映射解析到同一最内层源码位置 {attr.source_file}:{attr.source_line}，"
        f"且非内联归属函数同为 {attr.owning_symbol}。",
        f"覆盖 {len(builds)} 个构建: {', '.join(builds)}；"
        f"对应源码版本: {', '.join(revisions)}。",
    ]
    if uncorroborated:
        rationale_lines.append(
            "以下成员的报告自带内联层次缺失或与映射链不完全一致，已保留映射解析结论并标注 "
            "report_corroborated=false: " + ", ".join(uncorroborated)
        )
    else:
        rationale_lines.append("全部成员的报告自带候选符号与内联层次均与映射链交叉印证通过。")

    return {
        "group_id": _group_id(group.fault_category, attr.key()),
        "fault_category": group.fault_category,
        "stable_attribution": attr.as_dict(),
        "member_count": len(members),
        "builds": builds,
        "source_revisions": revisions,
        "representative_report_id": representative.report_id,
        "representative_evidence": representative.as_dict(),
        "merge_rationale": rationale_lines,
        "members": [m.as_dict() for m in members],
    }
