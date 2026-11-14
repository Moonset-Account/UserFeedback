"""Render analysis results as a JSON-ready dict and as human-readable text."""

from typing import List


def build_report(analysis: dict, sources: dict) -> dict:
    """Assemble the final JSON-serialisable report."""
    return {
        "report": "patch-coverage-matrix",
        "build": analysis["build"],
        "generated_from": sources,
        "summary": analysis["summary"],
        "entries": analysis["entries"],
        "uncovered_entries": analysis["uncovered_entries"],
        "observed_checkpoints": analysis["observed_checkpoints"],
        "unattributable_traces": analysis["unattributable_traces"],
    }


def _fmt_nearest(nearest) -> str:
    if nearest is None:
        return "none (no observed checkpoint reachable from this entry)"
    path = " -> ".join(nearest["path"])
    return (
        f"{nearest['id']} (module {nearest['module']}, "
        f"distance {nearest['distance']}, path {path})"
    )


def render_text(report: dict) -> str:
    lines: List[str] = []
    s = report["summary"]
    lines.append(f"Patch coverage matrix — build {report['build']}")
    lines.append(
        "entries: {total}  observed_covered: {cov}  statically_reachable: {reach}  "
        "unattributable: {unatt}  no_reachable_checkpoint: {noreach}".format(
            total=s["total_entries"],
            cov=s["observed_covered"],
            reach=s["statically_reachable"],
            unatt=s["unattributable"],
            noreach=s["no_reachable_checkpoint"],
        )
    )
    lines.append(
        f"checkpoints observed: {s['observed_checkpoints']}/{s['total_checkpoints']}  "
        f"unattributable trace records: {s['unattributable_trace_records']}"
    )
    lines.append("")
    for e in report["entries"]:
        lines.append(f"[{e['status']}] {e['id']} {e['name']} (module {e['module']})")
        lines.append(f"  reachable checkpoints: {', '.join(e['reachable_checkpoints']) or '(none)'}")
        if e["coverage_records"]:
            covered = ", ".join(
                f"{r['checkpoint_id']} (test {r['test_id']})" for r in e["coverage_records"]
            )
            lines.append(f"  covered: {covered}")
        if e["missing_checkpoints"]:
            lines.append(f"  missing: {', '.join(e['missing_checkpoints'])}")
        for rec in e["invalid_traces"]:
            lines.append(
                f"  unattributable trace: test {rec['test_id']} checkpoint "
                f"{rec['checkpoint_id']} reason={rec['reason']} ({rec['detail']})"
            )
        if e["status"] in ("statically_reachable", "unattributable"):
            lines.append(
                "  nearest observed checkpoint: "
                + _fmt_nearest(e["nearest_observed_checkpoint"])
            )
        lines.append("")
    lines.append("Uncovered entries (need a regression case):")
    if report["uncovered_entries"]:
        by_id = {e["id"]: e for e in report["entries"]}
        for entry_id in report["uncovered_entries"]:
            e = by_id[entry_id]
            lines.append(
                f"  {e['id']} {e['name']} [{e['status']}] — nearest observed checkpoint: "
                + _fmt_nearest(e["nearest_observed_checkpoint"])
            )
    else:
        lines.append("  (none)")
    if report["unattributable_traces"]:
        lines.append("")
        lines.append("Unattributable trace records (no owning entry):")
        for rec in report["unattributable_traces"]:
            lines.append(
                f"  test {rec['test_id']} entry={rec['entry_id']} "
                f"checkpoint={rec['checkpoint_id']} reason={rec['reason']} ({rec['detail']})"
            )
    return "\n".join(lines)
