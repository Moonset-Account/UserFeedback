"""Rendering of :class:`~release_check.checker.Report` values.

Two renderings are provided: a human-readable text form for terminals and a
stable JSON document for machine consumers.  Both are sorted, so equal
specifications always produce byte-identical output.
"""

from __future__ import annotations

import json
from typing import Iterable

from .checker import Finding, Report

__all__ = ["render_text", "render_json"]


def _render_fields(values: Iterable[str]) -> str:
    rendered = ", ".join(values)
    return rendered if rendered else "-"


def _render_finding(finding: Finding) -> list[str]:
    lines = [
        f"[error] {finding.code}",
        f"  implementation: {finding.implementation}",
        f"  entry:          {finding.entry}",
        f"  identity:       {finding.identity}",
        f"  detail:         {finding.message}",
    ]
    if finding.allowed:
        lines.append(f"  allowed:        {_render_fields(finding.allowed)}")
    if finding.actual:
        lines.append(f"  actual:         {_render_fields(finding.actual)}")
    if finding.origins:
        lines.append(f"  constrained by: {_render_fields(finding.origins)}")
    return lines


def render_text(report: Report, *, title: str = "release check") -> str:
    """Render ``report`` as plain text ending with a summary line."""

    lines = [f"{title}: {report.status}", ""]

    if report.findings:
        for finding in report.findings:
            lines.extend(_render_finding(finding))
            lines.append("")
    else:
        lines.append("no findings")
        lines.append("")

    if report.pending:
        lines.append("undecided entries")
        for entry in report.pending:
            missing = ", ".join(entry.missing)
            lines.append(
                f"  {entry.entry}: no declaration on public interface(s) {missing}"
            )
        lines.append("")

    lines.append(
        f"summary: status={report.status} errors={len(report.findings)} "
        f"pending={len(report.pending)}"
    )
    return "\n".join(lines) + "\n"


def render_json(report: Report) -> str:
    """Render ``report`` as a stable, indented JSON document."""

    document = report.to_dict()
    document["summary"] = {
        "status": report.status,
        "errors": len(report.findings),
        "pending": len(report.pending),
    }
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
