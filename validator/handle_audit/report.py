"""Text and JSON rendering of audit reports."""

import json

from .events import Event
from .findings import AuditReport
from .order import OrderGraph


def render_text(report: AuditReport, events: list[Event],
                graph: OrderGraph) -> str:
    by_id: dict[str, Event] = {e.event_id: e for e in events}
    lines: list[str] = []
    lines.append(f"VERDICT: {report.verdict.value}")
    lines.append(
        "summary: "
        + ", ".join(f"{k}={v}" for k, v in sorted(report.summary.items()))
    )
    lines.append(
        "stats: " + ", ".join(f"{k}={v}" for k, v in report.stats.items())
    )
    if report.structural_issues:
        lines.append("")
        lines.append("Structural issues:")
        for issue in report.structural_issues:
            lines.append(f"  - {issue}")

    if not report.findings:
        lines.append("")
        lines.append(
            "Every committed object is proven identical to a validated, "
            "still-valid authorization of the same task and handle."
        )
        return "\n".join(lines)

    lines.append("")
    lines.append("Findings:")
    for i, f in enumerate(report.findings, 1):
        lines.append(
            f"  [{i}] {f.verdict.value} {f.rule_id} "
            f"events={f.events}"
        )
        lines.append(f"      {f.message}")
        if f.unknown_reason:
            lines.append(f"      reason: {f.unknown_reason}")
        if f.evidence:
            lines.append("      order evidence:")
            for piece in f.evidence:
                if piece in by_id:
                    lines.append(f"        {by_id[piece].label()}")
                else:
                    lines.append(f"        {piece}")
    return "\n".join(lines)


def render_json(report: AuditReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)
    # sort_keys keeps output stable regardless of record arrival order
