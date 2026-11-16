"""Output formatters for the Secret/Config Diff Scanner.

Generates JSON, Markdown, HTML, and SARIF reports from ScanResult objects.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from html import escape as html_escape

from . import __version__


def _markdown_cell(value) -> str:
    return html_escape(str(value)).replace("|", "\\|").replace("`", "\\`").replace("\r", "").replace("\n", "<br>")


def format_json(result: "ScanResult") -> str:
    """Format scan results as JSON."""
    data = {
        "scanner": "secret-config-diff-scanner",
        "version": __version__,
        "timestamp": result.timestamp,
        "input_type": result.input_type,
        "input_source": result.input_source,
        "summary": result.summary,
        "findings": [
            {
                "type": f.type,
                "pattern_name": f.pattern_name,
                "severity": f.severity,
                "file": f.file,
                "line": f.line,
                "matched_text": "[REDACTED]",
                "line_content": "[REDACTED]",
                "rule": f.rule,
                "rule_id": f.rule_id,
                "category": f.category,
                "confidence": f.confidence,
                "remediation": f.remediation,
                "fingerprint": f.fingerprint,
            }
            for f in result.findings
        ],
        "config_changes": [
            {
                "type": c.type,
                "file": c.file,
                "severity": c.severity,
                "change_type": c.change_type,
                "description": c.description,
            }
            for c in result.config_changes
        ],
    }
    if result.vulnerability_findings:
        data["vulnerability_findings"] = result.vulnerability_findings
    return json.dumps(data, indent=2)


def format_markdown(result: "ScanResult") -> str:
    """Format scan results as Markdown."""
    lines = []
    lines.append(f"# Secret/Config Diff Scan Report")
    lines.append("")
    lines.append(f"**Scanner:** secret-config-diff-scanner v{__version__}")
    lines.append(f"**Timestamp:** {_markdown_cell(result.timestamp)}")
    lines.append(f"**Input:** {_markdown_cell(result.input_type)} — {_markdown_cell(result.input_source)}")
    lines.append("")

    # Summary
    s = result.summary
    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Metric | Count |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total findings | {s['total_findings']} |")
    lines.append(f"| Critical | {s['critical']} |")
    lines.append(f"| High | {s['high']} |")
    lines.append(f"| Medium | {s['medium']} |")
    lines.append(f"| Low | {s['low']} |")
    lines.append(f"| Secret findings | {s['secret_findings']} |")
    lines.append(f"| Policy findings | {s.get('policy_findings', 0)} |")
    lines.append(f"| Config changes | {s['config_findings']} |")
    lines.append(f"| Baseline suppressed | {s.get('baseline_suppressed', 0)} |")
    lines.append(f"| Baseline total | {s.get('baseline_total', 0)} |")
    lines.append(f"| Baseline matched | {s.get('baseline_matched', 0)} |")
    lines.append(f"| Baseline stale | {s.get('baseline_stale', 0)} |")
    lines.append("")

    # Secret findings
    secret_findings = [f for f in result.findings if f.type == "secret"]
    if secret_findings:
        lines.append("## Secret Findings")
        lines.append("")
        lines.append("| Rule ID | Severity | Pattern | File | Line | Matched |")
        lines.append("|---------|----------|---------|------|------|---------|")
        for f in secret_findings:
            lines.append(f"| {_markdown_cell(f.rule_id)} | {_markdown_cell(f.severity)} | {_markdown_cell(f.pattern_name)} | {_markdown_cell(f.file)} | {f.line} | [REDACTED] |")
        lines.append("")

    policy_findings = [f for f in result.findings if f.type == "policy"]
    if policy_findings:
        lines.extend(["## Policy Findings", "", "| Rule ID | Severity | Category | File | Line | Remediation |", "|---------|----------|----------|------|------|-------------|"])
        for f in policy_findings:
            lines.append(f"| {_markdown_cell(f.rule_id)} | {_markdown_cell(f.severity)} | {_markdown_cell(f.category)} | {_markdown_cell(f.file)} | {f.line} | {_markdown_cell(f.remediation)} |")
        lines.append("")

    # Config changes
    if result.config_changes:
        lines.append("## Config Changes")
        lines.append("")
        lines.append("| Severity | File | Change Type | Description |")
        lines.append("|----------|------|-------------|-------------|")
        for c in result.config_changes:
            lines.append(f"| {_markdown_cell(c.severity)} | {_markdown_cell(c.file)} | {_markdown_cell(c.change_type)} | {_markdown_cell(c.description)} |")
        lines.append("")

    # Remediation
    if result.findings or result.config_changes:
        lines.append("## Remediation")
        lines.append("")
        if secret_findings:
            lines.append("- **Secrets detected:** Rotate compromised credentials immediately.")
            lines.append("- Use environment variables or secret managers instead of hardcoding secrets.")
            lines.append("- Add patterns to `.secretsallowlist` for known-safe test values.")
        if result.config_changes:
            lines.append("- **Config changes detected:** Review each config change carefully.")
            lines.append("- Pay special attention to auth, security, and infrastructure configs.")
        lines.append("")

    return "\n".join(lines)


def format_html(result: "ScanResult") -> str:
    """Format scan results as HTML."""
    s = result.summary

    # Severity color mapping
    severity_colors = {
        "critical": "#dc3545",
        "high": "#fd7e14",
        "medium": "#ffc107",
        "low": "#17a2b8",
    }

    secret_findings = [f for f in result.findings if f.type == "secret"]

    html_parts = [
        "<!DOCTYPE html>",
        "<html lang='en'>",
        "<head>",
        "<meta charset='UTF-8'>",
        f"<title>Secret/Config Scan Report — {html_escape(result.input_source)}</title>",
        "<style>",
        "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 960px; margin: 0 auto; padding: 20px; }",
        "h1 { color: #1a1a1a; }",
        ".summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin: 20px 0; }",
        ".stat { background: #f8f9fa; border-radius: 6px; padding: 12px; text-align: center; }",
        ".stat-value { font-size: 1.8em; font-weight: bold; }",
        ".stat-label { font-size: 0.85em; color: #666; }",
        "table { width: 100%; border-collapse: collapse; margin: 10px 0; }",
        "th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }",
        "th { background: #f0f0f0; font-weight: 600; }",
        ".severity-badge { display: inline-block; padding: 2px 8px; border-radius: 3px; color: white; font-size: 0.85em; }",
        ".remediation { background: #f8f9fa; border-left: 4px solid #007bff; padding: 12px 16px; margin: 20px 0; }",
        "code { background: #f4f4f4; padding: 2px 4px; border-radius: 3px; }",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>🔐 Secret/Config Diff Scan Report</h1>",
        f"<p><strong>Scanner:</strong> secret-config-diff-scanner v{__version__}</p>",
        f"<p><strong>Timestamp:</strong> {html_escape(result.timestamp)}</p>",
        f"<p><strong>Input:</strong> {html_escape(result.input_type)} — <code>{html_escape(result.input_source)}</code></p>",
        "",
        "<div class='summary'>",
        f"  <div class='stat'><div class='stat-value'>{s['total_findings']}</div><div class='stat-label'>Total</div></div>",
        f"  <div class='stat'><div class='stat-value' style='color:{severity_colors.get('critical', '#333')}'>{s['critical']}</div><div class='stat-label'>Critical</div></div>",
        f"  <div class='stat'><div class='stat-value' style='color:{severity_colors.get('high', '#333')}'>{s['high']}</div><div class='stat-label'>High</div></div>",
        f"  <div class='stat'><div class='stat-value' style='color:{severity_colors.get('medium', '#333')}'>{s['medium']}</div><div class='stat-label'>Medium</div></div>",
        f"  <div class='stat'><div class='stat-value' style='color:{severity_colors.get('low', '#333')}'>{s['low']}</div><div class='stat-label'>Low</div></div>",
        f"  <div class='stat'><div class='stat-value'>{s.get('baseline_total', 0)}</div><div class='stat-label'>Baseline total</div></div>",
        f"  <div class='stat'><div class='stat-value'>{s.get('baseline_matched', 0)}</div><div class='stat-label'>Baseline matched</div></div>",
        f"  <div class='stat'><div class='stat-value'>{s.get('baseline_stale', 0)}</div><div class='stat-label'>Baseline stale</div></div>",
        "</div>",
    ]

    if secret_findings:
        html_parts.append("<h2>🔒 Secret Findings</h2>")
        html_parts.append("<table><tr><th>Rule ID</th><th>Severity</th><th>Pattern</th><th>File</th><th>Line</th><th>Evidence</th></tr>")
        for f in secret_findings:
            color = severity_colors.get(f.severity, "#666")
            html_parts.append(
                f"<tr><td>{html_escape(f.rule_id)}</td><td><span class='severity-badge' style='background:{color}'>{f.severity}</span></td>"
                f"<td>{html_escape(f.pattern_name)}</td><td><code>{html_escape(f.file)}</code></td>"
                f"<td>{f.line}</td><td><code>[REDACTED]</code></td></tr>"
            )
        html_parts.append("</table>")

    policy_findings = [f for f in result.findings if f.type == "policy"]
    if policy_findings:
        html_parts.append("<h2>🛡️ Policy Findings</h2>")
        html_parts.append("<table><tr><th>Rule ID</th><th>Severity</th><th>Category</th><th>File</th><th>Line</th><th>Remediation</th></tr>")
        for f in policy_findings:
            color = severity_colors.get(f.severity, "#666")
            html_parts.append(f"<tr><td>{html_escape(f.rule_id)}</td><td><span class='severity-badge' style='background:{color}'>{html_escape(f.severity)}</span></td><td>{html_escape(f.category)}</td><td>{html_escape(f.file)}</td><td>{f.line}</td><td>{html_escape(f.remediation)}</td></tr>")
        html_parts.append("</table>")

    if result.config_changes:
        html_parts.append("<h2>⚙️ Config Changes</h2>")
        html_parts.append("<table><tr><th>Severity</th><th>File</th><th>Change</th><th>Description</th></tr>")
        for c in result.config_changes:
            color = severity_colors.get(c.severity, "#666")
            html_parts.append(
                f"<tr><td><span class='severity-badge' style='background:{color}'>{c.severity}</span></td>"
                f"<td><code>{html_escape(c.file)}</code></td><td>{html_escape(c.change_type)}</td>"
                f"<td>{html_escape(c.description)}</td></tr>"
            )
        html_parts.append("</table>")

    if result.findings or result.config_changes:
        html_parts.append("<div class='remediation'>")
        html_parts.append("<h2>🛠️ Remediation</h2>")
        if secret_findings:
            html_parts.append("<p><strong>Secrets detected:</strong> Rotate compromised credentials immediately. Use environment variables or secret managers. Add patterns to <code>.secretsallowlist</code> for known-safe values.</p>")
        if result.config_changes:
            html_parts.append("<p><strong>Config changes detected:</strong> Review each config change carefully. Pay special attention to auth, security, and infrastructure configs.</p>")
        html_parts.append("</div>")

    html_parts.extend(["</body>", "</html>"])
    return "\n".join(html_parts)


def _github_escape(value: object) -> str:
    """Escape GitHub workflow command fields in the mandated order."""
    return (str(value).replace("%", "%25").replace("\r", "%0D")
            .replace("\n", "%0A").replace(":", "%3A").replace(",", "%2C"))


def format_github(result: "ScanResult") -> str:
    """Format secret-safe GitHub Actions annotations."""
    levels = {"critical": "error", "high": "error", "medium": "warning", "low": "notice"}
    lines = []
    for finding in result.findings:
        label = "potential secret detected" if finding.type == "secret" else "policy risk detected"
        message = _github_escape(f"{finding.rule_id} {finding.pattern_name}: {label} ({finding.severity}) [{finding.fingerprint}]")
        lines.append(f"::{levels.get(finding.severity, 'warning')} file={_github_escape(finding.file)},line={max(1, finding.line)}::{message}")
    for change in result.config_changes:
        message = _github_escape(f"Config {change.change_type}: {change.description} ({change.severity})")
        lines.append(f"::{levels.get(change.severity, 'warning')} file={_github_escape(change.file)},line=1::{message}")
    return "\n".join(lines)


# SARIF severity mapping
_SARIF_SEVERITY_MAP = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
}

_SARIF_SECURITY_SEVERITY = {
    "critical": "9.5",
    "high": "8.0",
    "medium": "6.0",
    "low": "3.0",
}


def _sarif_rule_id(prefix: str, name: str) -> str:
    """Return a stable SARIF-safe rule id, preserving existing safe names."""
    safe = re.sub(r"[^A-Za-z0-9._/-]+", "_", name).strip("_") or "RULE"
    if safe != name:
        safe = f"{safe}-{hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]}"
    return f"{prefix}/{safe}"


def format_sarif(result: "ScanResult") -> str:
    """Format scan results as SARIF (Static Analysis Results Interchange Format).

    SARIF is an OASIS standard for static analysis results, widely used
    by GitHub Code Scanning, Azure DevOps, and other CI platforms.
    """
    # Build rules from findings and config changes
    rules = []
    rule_indices = {}
    results_list = []

    # Process secret findings
    for f in result.findings:
        rule_id = f.rule_id if f.rule_id != "CRT-SEC-000" else _sarif_rule_id("SCDS", f.pattern_name)
        if rule_id not in rule_indices:
            idx = len(rules)
            rule_indices[rule_id] = idx
            rules.append({
                "id": rule_id,
                "name": f.pattern_name,
                "shortDescription": {
                    "text": f.rule if f.rule else f.pattern_name,
                },
                "fullDescription": {
                    "text": f"{f.category} rule: {f.pattern_name} (severity: {f.severity}, confidence: {f.confidence})",
                },
                "help": {
                    "text": f.remediation,
                },
                "properties": {
                    "security-severity": _SARIF_SECURITY_SEVERITY.get(f.severity, "5.0"),
                },
            })

        sarif_level = _SARIF_SEVERITY_MAP.get(f.severity, "warning")
        results_list.append({
            "ruleId": rule_id,
            "ruleIndex": rule_indices[rule_id],
            "level": sarif_level,
            "message": {
                "text": f"{f.category} finding: {f.pattern_name} in {f.file}:{f.line}",
            },
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {
                            "uri": f.file,
                        },
                        "region": {
                            "startLine": f.line if f.line > 0 else 1,
                        },
                    },
                },
            ],
            "properties": {"category": f.category, "severity": f.severity, "confidence": f.confidence, "fingerprint": f.fingerprint},
        })

    # Process vulnerability findings
    for finding in result.vulnerability_findings:
        rule_id = "CRT-VULN-" + re.sub(r"[^A-Za-z0-9._-]+", "_", finding["advisory_id"]).strip("_")
        if rule_id not in rule_indices:
            rule_indices[rule_id] = len(rules)
            rules.append({
                "id": rule_id,
                "name": finding["advisory_id"],
                "shortDescription": {"text": f"Vulnerability {finding['advisory_id']}"},
                "fullDescription": {"text": finding["explanation"]},
                "help": {"text": "Review the affected dependency and upgrade to a fixed version when available."},
                "properties": {"category": "vulnerability", "confidence": finding["confidence"], "severity": finding["severity"]},
            })
        results_list.append({
            "ruleId": rule_id,
            "ruleIndex": rule_indices[rule_id],
            "level": "warning",
            "message": {"text": f"Vulnerability {finding['advisory_id']} affects {finding['component_purl']}"},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": finding["manifest_path"]},
                    "region": {"startLine": 1},
                },
            }],
            "properties": {
                "category": "vulnerability",
                "confidence": finding["confidence"],
                "severity": finding["severity"],
                "fingerprint": finding["fingerprint"],
                "snapshot_id": finding["snapshot_id"],
            },
        })

    # Process config changes
    for c in result.config_changes:
        rule_id = _sarif_rule_id("SCDS", f"CONFIG_{c.change_type.upper()}")
        if rule_id not in rule_indices:
            idx = len(rules)
            rule_indices[rule_id] = idx
            rules.append({
                "id": rule_id,
                "name": f"Config {c.change_type}",
                "shortDescription": {
                    "text": c.description,
                },
                "fullDescription": {
                    "text": f"Config change: {c.description} (severity: {c.severity})",
                },
                "help": {
                    "text": "Review the configuration change and confirm its security and deployment impact before merging.",
                },
                "properties": {
                    "security-severity": _SARIF_SECURITY_SEVERITY.get(c.severity, "5.0"),
                },
            })

        sarif_level = _SARIF_SEVERITY_MAP.get(c.severity, "warning")
        results_list.append({
            "ruleId": rule_id,
            "ruleIndex": rule_indices[rule_id],
            "level": sarif_level,
            "message": {
                "text": f"Config change: {c.description} in {c.file}",
            },
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {
                            "uri": c.file,
                        },
                        "region": {
                            "startLine": 1,
                        },
                    },
                },
            ],
        })

    sarif = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "secret-config-diff-scanner",
                        "version": __version__,
                        "semanticVersion": __version__,
                        "informationUri": "https://coderisktools.store",
                        "rules": rules,
                    },
                },
                "results": results_list,
                "originalUriBaseIds": {"%SRCROOT%": {"uri": "./"}},
            },
        ],
    }

    return json.dumps(sarif, indent=2)