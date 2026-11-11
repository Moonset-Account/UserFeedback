"""报告渲染：机器可读 JSON 与人工可读纯文本，两种输出均为确定性内容。"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Dict, List, Optional, Sequence

from .engine import ChangeEntry, Adjudicator, summarize
from .models import (
    Bom,
    DISPOSITION_LABELS_ZH,
    DISPOSITIONS,
    Finding,
)


def _finding_to_dict(finding: Finding) -> OrderedDict:
    return OrderedDict([
        ("release_id", finding.release_id),
        ("bom_ref", finding.bom_ref),
        ("ecosystem", finding.ecosystem),
        ("namespace", finding.namespace),
        ("name", finding.name),
        ("version", finding.version),
        ("vuln_id", finding.vuln_id),
        ("disposition", finding.disposition),
        ("disposition_zh", DISPOSITION_LABELS_ZH[finding.disposition]),
        ("evidence", list(finding.evidence)),
        ("reasons", list(finding.reasons)),
    ])


def _component_rollup(boms: Sequence[Bom], findings: Sequence[Finding]) -> List[OrderedDict]:
    """组件级汇总：同一组件涉及多个漏洞时按最严重处置归并。

    优先级 affected > needs_verification > fixed > not_affected；
    没有任何漏洞条目的组件标记为 no_applicable_advisory。
    """
    priority = {
        "affected": 0,
        "needs_verification": 1,
        "fixed": 2,
        "not_affected": 3,
    }
    index: Dict[tuple, Finding] = {}
    for finding in findings:
        key = (finding.release_id, finding.bom_ref)
        if key not in index or priority[finding.disposition] < priority[index[key].disposition]:
            index[key] = finding
    rows: List[OrderedDict] = []
    known = set(index)
    for bom in boms:
        for component in bom.components:
            key = (bom.release_id, component.ref)
            ns = f"{component.namespace}/" if component.namespace else ""
            label = f"{component.ecosystem}:{ns}{component.name}@{component.version}"
            if key in known:
                top = index[key]
                rows.append(OrderedDict([
                    ("release_id", bom.release_id),
                    ("bom_ref", component.ref),
                    ("component", label),
                    ("rollup_disposition", top.disposition),
                    ("rollup_disposition_zh", DISPOSITION_LABELS_ZH[top.disposition]),
                    ("evidence", top.evidence),
                ]))
            else:
                rows.append(OrderedDict([
                    ("release_id", bom.release_id),
                    ("bom_ref", component.ref),
                    ("component", label),
                    ("rollup_disposition", "no_applicable_advisory"),
                    ("rollup_disposition_zh", "无适用公告"),
                    ("evidence", []),
                ]))
    return rows


def _change_to_dict(change: ChangeEntry) -> OrderedDict:
    def label(d: Optional[str]) -> Optional[str]:
        return None if d is None else f"{d}({DISPOSITION_LABELS_ZH[d]})"

    return OrderedDict([
        ("release_id", change.key[0]),
        ("bom_ref", change.key[1]),
        ("vuln_id", change.vuln_id),
        ("component", change.component),
        ("before", label(change.before)),
        ("after", label(change.after)),
        ("reasons", list(change.reasons)),
    ])


def build_report(
    boms: Sequence[Bom],
    findings: Sequence[Finding],
    changes: Optional[Sequence[ChangeEntry]] = None,
    baseline_findings: Optional[Sequence[Finding]] = None,
    inputs: Optional[Dict[str, str]] = None,
) -> OrderedDict:
    report = OrderedDict()
    report["report_version"] = 1
    report["inputs"] = inputs or {}
    report["snapshots"] = [
        {"release_id": b.release_id, "snapshot_id": b.snapshot_id,
         "component_count": len(b.components)}
        for b in boms
    ]
    report["disposition_summary"] = summarize(findings)
    report["component_rollup"] = _component_rollup(boms, findings)
    report["findings"] = [_finding_to_dict(f) for f in findings]
    if changes is not None:
        report["change_report"] = {
            "change_count": len(changes),
            "baseline_disposition_summary":
                summarize(baseline_findings) if baseline_findings is not None else None,
            "changes": [_change_to_dict(c) for c in changes],
        }
    return report


def render_json(report: OrderedDict) -> str:
    # sort_keys=False 保留精心设计的字段顺序；ensure_ascii=False 保留中文
    return json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False)


def render_text(report: OrderedDict) -> str:
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("离线漏洞适用性裁决报告")
    lines.append("=" * 78)
    lines.append("输入文件:")
    for key, value in report["inputs"].items():
        lines.append(f"  - {key}: {value}")
    lines.append("")
    lines.append("清单快照:")
    for snap in report["snapshots"]:
        lines.append(
            f"  - 发行版本 {snap['release_id']} / 快照 {snap['snapshot_id']} "
            f"/ 组件 {snap['component_count']} 个"
        )
    lines.append("")
    lines.append("处置统计（按 组件×漏洞 条目）:")
    for disposition in DISPOSITIONS:
        zh = DISPOSITION_LABELS_ZH[disposition]
        lines.append(f"  - {zh:<4} ({disposition:<18}): "
                     f"{report['disposition_summary'].get(disposition, 0)}")
    lines.append("")
    lines.append("组件级汇总:")
    for row in report["component_rollup"]:
        lines.append(
            f"  [{row['release_id']}] {row['bom_ref']:<12} {row['component']:<46} "
            f"-> {row['rollup_disposition_zh']}"
        )
    lines.append("")
    lines.append("逐条裁决明细:")
    for finding in report["findings"]:
        lines.extend(_render_finding_text(finding))
    if "change_report" in report:
        change = report["change_report"]
        lines.append("")
        lines.append("-" * 78)
        lines.append(f"公告更新前后变化报告（共 {change['change_count']} 条）:")
        if change["baseline_disposition_summary"] is not None:
            before = change["baseline_disposition_summary"]
            after = report["disposition_summary"]
            lines.append("  统计对比:")
            for disposition in DISPOSITIONS:
                lines.append(
                    f"    {DISPOSITION_LABELS_ZH[disposition]:<4}: "
                    f"{before.get(disposition, 0)} -> {after.get(disposition, 0)}"
                )
        for item in change["changes"]:
            before = item["before"] or "（无条目）"
            after = item["after"] or "（无条目）"
            lines.append(
                f"  [{item['release_id']}] {item['component']} / {item['vuln_id']}: "
                f"{before} => {after}"
            )
            for reason in item["reasons"]:
                lines.append(f"      原因: {reason}")
    lines.append("=" * 78)
    return "\n".join(lines)


def _render_finding_text(finding: OrderedDict) -> List[str]:
    lines = [
        f"  [{finding['release_id']}] {finding['bom_ref']} "
        f"{finding['ecosystem']}:"
        f"{finding['namespace'] + '/' if finding['namespace'] else ''}"
        f"{finding['name']}@{finding['version']} "
        f"| 漏洞 {finding['vuln_id']} "
        f"| 处置: {finding['disposition_zh']}"
    ]
    evidence = ", ".join(finding["evidence"]) if finding["evidence"] else "无"
    lines.append(f"      证据编号: {evidence}")
    for reason in finding["reasons"]:
        lines.append(f"      原因: {reason}")
    return lines
