"""Human-readable Chinese reports for capacity on-call review."""

from __future__ import annotations

from typing import Any

from .report import AnalysisResult, ReissueAttribution, WindowReport
from .windows import MetricBounds


def fmt_bounds(bounds: MetricBounds, *, integer: bool = True) -> str:
    if integer:
        return f"{_num(bounds.lower)}/{_num(bounds.point)}/{_num(bounds.upper)}"
    return f"{bounds.lower:.3f}/{bounds.point:.3f}/{bounds.upper:.3f}"


def _num(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.3f}"


def format_window_table(result: AnalysisResult) -> str:
    lines = [
        "窗口 [start,end) | 到达 L/P/U | 限速 | 补发 | 完成 | 撤回 | 容量丢失 | 完成吞吐 RPS L/P/U(含缺失报告上限)"
    ]
    for w in result.windows:
        lines.append(
            f"W{w.index} [{_num(w.start)},{_num(w.end)}) | "
            f"{fmt_bounds(w.arrivals)} | "
            f"{fmt_bounds(w.rate_limited)} | "
            f"{fmt_bounds(w.reissues)} | "
            f"{fmt_bounds(w.completions)} | "
            f"{fmt_bounds(w.withdrawals)} | "
            f"{fmt_bounds(w.capacity_lost)} | "
            f"{fmt_bounds(w.throughput, integer=False)}"
        )
    lines.append("计数格式为 lower/point/upper；L=确定在窗口内，U=可能在窗口内。")
    return "\n".join(lines)


def format_reissue_journal(attribution: ReissueAttribution) -> str:
    lines = [
        f"补发事件 {attribution.reissue_event_id} / lineage={attribution.lineage_id} "
        f"/ count={attribution.count}",
        attribution.rule,
    ]
    original = attribution.original_window_index
    point = attribution.reissue_window.point_index
    lines.append(
        f"原始窗口=W{original if original is not None else '?'}，"
        f"补发点估计窗口=W{point}，可能窗口={attribution.reissue_window.possible_indices}"
    )
    for entry in attribution.entries:
        lines.append(
            f"  - W{entry.window_index} [{entry.view}/{entry.metric}] "
            f"count={entry.count} role={entry.role}; {entry.note}"
        )
    lines.append(
        f"朴素把补发当新到达会虚增 unique demand：{attribution.naive_new_arrival_overcount}"
    )
    return "\n".join(lines)


def format_incidents(result: AnalysisResult) -> str:
    lines: list[str] = []
    lines.append("主动撤回：")
    if not result.withdrawals:
        lines.append("  (none)")
    for incident in result.withdrawals:
        lines.append(
            f"  - {incident.incident_id} W{incident.point_window_index} "
            f"count={incident.count} client={incident.client_id} "
            f"time=[{incident.time_bounds.lower:.3f},"
            f"{incident.time_bounds.upper:.3f}] {incident.explanation}"
        )
    lines.append("容量丢失：")
    if not result.capacity_losses:
        lines.append("  (none)")
    for incident in result.capacity_losses:
        lines.append(
            f"  - {incident.incident_id} W{incident.point_window_index} "
            f"count={incident.count} via={incident.detected_by} "
            f"time=[{incident.time_bounds.lower:.3f},"
            f"{incident.time_bounds.upper:.3f}] {incident.explanation}"
        )
    lines.append("限速后未追偿/接入缺口（非主动撤回，默认非服务端容量丢失）：")
    if not getattr(result, "admission_gaps", None):
        lines.append("  (none)")
    for incident in result.admission_gaps:
        lines.append(
            f"  - {incident.incident_id} W{incident.point_window_index} "
            f"count={incident.count} via={incident.detected_by} "
            f"time=[{incident.time_bounds.lower:.3f},"
            f"{incident.time_bounds.upper:.3f}] {incident.explanation}"
        )
    return "\n".join(lines)


def format_report(result: AnalysisResult) -> str:
    summary = result.summary
    lines = [
        "容量值班排队/追偿对账报告",
        "=" * 32,
        "摘要：" + ", ".join(f"{k}={v}" for k, v in summary.items()),
        "",
        format_window_table(result),
        "",
        format_incidents(result),
        "",
        "跨窗口补发计量：",
    ]
    if not result.cross_window_reissues:
        lines.append("  (none)")
    for attribution in result.cross_window_reissues:
        lines.append(format_reissue_journal(attribution))
        lines.append("")
    if result.issues:
        lines.append("数据问题：")
        for issue in result.issues:
            lines.append(f"  - [{issue.code}] {issue.message}")
    return "\n".join(lines)
