"""Assemble the machine-readable report and a human-readable text view."""

from __future__ import annotations

from .analysis import Engine
from .loader import load_graph, load_rule_table
from .verdict import (
    NEWLY_ALLOWED,
    NEWLY_BLOCKED,
    UNCHANGED,
    UNDETERMINED_BUCKET,
    UNREACHABLE_BUCKET,
    classify,
)

_BUCKET_TITLES = {
    NEWLY_ALLOWED: "NEWLY ALLOWED (rule update opens a previously blocked path)",
    NEWLY_BLOCKED: "NEWLY BLOCKED (rule update revokes an existing allowance)",
    UNCHANGED: "UNCHANGED VERDICT",
    UNDETERMINED_BUCKET: "UNDETERMINED (graph incomplete; cannot decide)",
    UNREACHABLE_BUCKET: "UNREACHABLE (no tainted data reaches the sink)",
}


def run_check(graph_path: str, old_rules_path: str, new_rules_path: str,
              roots=()):
    graph = load_graph(graph_path)
    old_rules = load_rule_table(old_rules_path)
    new_rules = load_rule_table(new_rules_path)
    engine = Engine(graph)
    node_envs = engine.analyze(declared_roots=roots)
    comparison = classify(graph, old_rules, new_rules, node_envs)
    return build_report(graph, old_rules, new_rules, comparison)


def build_report(graph, old_rules, new_rules, comparison) -> dict:
    counts = {b: 0 for b in _BUCKET_TITLES}
    for item in comparison["items"]:
        counts[item["classification"]] += 1
    return {
        "graph_id": graph.graph_id,
        "rule_versions": {
            "old": old_rules.version,
            "new": new_rules.version,
        },
        "rule_diff": {
            "added_pairs": comparison["added_rule_pairs"],
            "removed_pairs": comparison["removed_rule_pairs"],
        },
        "summary": counts,
        "incompleteness_notes": list(graph.assumptions),
        "items": comparison["items"],
    }


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

def render_text(report: dict) -> str:
    lines = []
    lines.append(f"Pre-release sanitizer rule check — graph {report['graph_id']}")
    lines.append(
        f"Rule versions: {report['rule_versions']['old']} -> "
        f"{report['rule_versions']['new']}")
    diff = report["rule_diff"]
    lines.append(
        f"Rule tuples added: {diff['added_pairs'] or 'none'}; "
        f"removed: {diff['removed_pairs'] or 'none'}")
    lines.append("Summary: " + ", ".join(
        f"{bucket}={report['summary'][bucket]}"
        for bucket in _BUCKET_TITLES))
    lines.append("")

    for bucket, title in _BUCKET_TITLES.items():
        bucket_items = [i for i in report["items"]
                        if i["classification"] == bucket]
        if not bucket_items:
            continue
        lines.append(f"== {title} ({len(bucket_items)}) ==")
        for item in bucket_items:
            lines.extend(_render_item(item))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_item(item) -> list:
    out = [
        f"- {item['name']}  sink={item['sink']} field={item['field']} "
        f"category={item['category']}  "
        f"verdict: {item['old_verdict']} -> {item['new_verdict']}"
    ]
    if item["minimal_rule_evidence"]:
        ev = ", ".join(
            f"{e['change']} ({e['sanitizer']}, {e['category']})"
            for e in item["minimal_rule_evidence"]
        )
        out.append(f"    minimal rule-change evidence: {ev}")
    if item["witness_path"]:
        out.append(f"    shortest witness: {' -> '.join(item['witness_path'])}")
    for alt in item["alternatives"]:
        status = "OPEN" if alt["path_is_open"] else "covered"
        unk = " [UNKNOWN ORIGIN]" if alt["unknown"] else ""
        sans = ",".join(alt["sanitizers_on_path"]) or "-"
        eff = ",".join(alt["effective_sanitizers"]) or "-"
        origins = ",".join(alt["origins"]) or "-"
        out.append(
            f"    branch alternative [{status}{unk}] origins={origins} "
            f"sanitizers_on_path=({sans}) effective_here=({eff})")
    san_steps = [s for s in item["step_explanation"]
                 if s.get("sanitizers_applied_here")]
    if san_steps:
        out.append("    where sanitization takes effect:")
        for step in san_steps:
            fields = ",".join(step["observed_fields"])
            out.append(
                f"      * {step['node']} ({step['function']}/{step['kind']}): "
                f"applies {', '.join(step['sanitizers_applied_here'])} "
                f"to {fields}")
    if item["incomplete_reasons"] and item["classification"] == UNDETERMINED_BUCKET:
        for reason in item["incomplete_reasons"]:
            out.append(f"    incompleteness: {reason}")
    return out
