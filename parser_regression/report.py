"""Renderers: machine-readable JSON and a review-friendly Markdown table."""

import json
from typing import List

from .compare import SECTION_ORDER

SECTION_TITLES = {
    "new_failures": "New failures",
    "fixed": "Fixed",
    "persistent_failures": "Persistent failures",
    "unstable": "Unstable",
    "not_executed": "Not executed",
    "missing_from_current": "Missing from current export",
    "scope_mismatch": "Content seen only under another mode/environment",
    "needs_confirmation": "Needs confirmation (duplicate attempt numbers)",
}


def _join(values: List[str]) -> str:
    return ", ".join(values) if values else "-"


def _md_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _sample_cell(row: dict) -> str:
    sample = row.get("sample")
    if isinstance(sample, list):
        sample = ", ".join(str(item) for item in sample)
    sample = str(sample)
    if row.get("renamed"):
        baseline_sample = row.get("baseline_sample")
        if isinstance(baseline_sample, list):
            baseline_sample = ", ".join(str(item) for item in baseline_sample)
        return f"{sample} (was {baseline_sample})"
    return sample


def _content_cell(row: dict) -> str:
    summary = row.get("summary")
    if isinstance(summary, list):
        summary = ", ".join(str(item) for item in summary)
    return str(summary) if summary else "-"


def render_markdown(summary: dict) -> str:
    lines: List[str] = []
    baseline = summary["baseline"]
    current = summary["current"]
    lines.append("# Parser regression summary")
    lines.append("")
    lines.append(f"- Baseline build: `{baseline['build_id']}` "
                 f"(source: {baseline['source']}; file: {baseline['path']})")
    lines.append(f"- Current build: `{current['build_id']}` "
                 f"(source: {current['source']}; file: {current['path']})")
    lines.append(f"- Groups compared: {summary['compared_groups']}")
    counts = summary["counts"]
    lines.append(
        "- Counts: "
        f"new failures {counts['new_failures']}, fixed {counts['fixed']}, "
        f"persistent {counts['persistent_failures']}, unstable {counts['unstable']}, "
        f"not executed {counts['not_executed']}, "
        f"missing {counts['missing_from_current']}, "
        f"scope mismatch {counts['scope_mismatch']}, "
        f"needs confirmation {counts['needs_confirmation']}, "
        f"stable passes {counts['stable_passes']}"
    )
    lines.append("")
    lines.append(
        "A row is comparable only within the same content identity, parse mode "
        "and environment. Each row lists the sample, its content summary, parse "
        "mode, build id, environment, attempt numbers, failure categories and "
        "timeout origin."
    )
    lines.append("")

    for section in SECTION_ORDER:
        rows = summary["sections"][section]
        lines.append(f"## {SECTION_TITLES[section]} ({len(rows)})")
        lines.append("")
        if not rows:
            lines.append("_None._")
            lines.append("")
            continue
        lines.append(
            "| Sample | Content summary | Parse mode | Build | Environment | "
            "Attempts | Failure category | Timeout origin | Notes |"
        )
        lines.append(
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
        )
        for row in rows:
            categories = row.get("failure_categories") or []
            category_text = _join(categories)
            notes = list(row.get("notes") or [])
            # A pending-confirmation row has no settled outcome, so labelling
            # its categories "new" would imply a classification it does not have.
            new_categories = row.get("new_failure_categories") or []
            if new_categories and row.get("status") != "pending_confirmation":
                notes.append("new failure category in this build: "
                             + _join(new_categories))
            attempts = [str(value) for value in row.get("attempts") or []]
            lines.append(
                "| "
                + " | ".join([
                    _md_escape(_sample_cell(row)),
                    _md_escape(_content_cell(row)),
                    _md_escape(str(row.get("parse_mode", ""))),
                    _md_escape(str(row.get("build_id") or "-")),
                    _md_escape(str(row.get("environment", ""))),
                    _md_escape(_join(attempts)),
                    _md_escape(category_text),
                    _md_escape(str(row.get("timeout", "no_timeout"))),
                    _md_escape("; ".join(notes) or "-"),
                ])
                + " |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_json(summary: dict) -> str:
    return json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
