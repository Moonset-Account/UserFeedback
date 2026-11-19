"""Command line entry point: ``python -m vlancheck.cli config.json``."""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .loader import load_config
from .model import AnalysisReport, BoundaryEvent, DropEvent
from .analyzer import analyze


def _format_port(ref) -> str:
    return f"{ref[0]}/{ref[1]}"


def _format_domain(ref) -> str:
    return f"{ref[0]}:vlan{ref[1]}"


def format_text_report(report: AnalysisReport) -> str:
    lines: List[str] = []
    for root in sorted(report.results):
        result = report.results[root]
        reached = ", ".join(
            _format_domain(domain) for domain in sorted(result.reached_domains)
        )
        lines.append(f"ROOT {_format_domain(root)} reaches: {reached}")
        for event in result.boundary_events:
            tag_text = (
                f"tag {event.sent_tag}"
                if event.sent_tag is not None
                else "untagged (stripped)"
            )
            lines.append(
                f"  - {_format_port(event.egress_port)} -> "
                f"{_format_port(event.ingress_port)} [{tag_text}] "
                f"received as {_format_domain(event.received_domain)} "
                f"[{event.status}]"
            )
        if result.first_leak is not None:
            leak = result.first_leak
            lines.append(
                f"  ! FIRST MISINTERPRETATION at {_format_port(leak.ingress_port)} "
                f"-> {_format_domain(leak.received_domain)} "
                f"(expected {_format_domain(leak.expected_domain)})"
            )

    for drop in report.drops:
        lines.append(
            f"DROP root {_format_domain(drop.root)}: tag {drop.rejected_tag} "
            f"rejected by {_format_port(drop.ingress_port)} "
            f"(sent from {_format_port(drop.egress_port)})"
        )

    if not lines:
        lines.append("no traffic possible: topology without links or domains")
    lines.append(
        f"SUMMARY roots={len(report.results)} "
        f"leaking_roots={len(report.leaking_roots)} drops={len(report.drops)}"
    )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline VLAN isolation-domain configuration checker"
    )
    parser.add_argument("config", help="path to a JSON topology configuration")
    parser.add_argument(
        "--json", action="store_true", help="emit the full report as JSON"
    )
    args = parser.parse_args(argv)

    topology = load_config(args.config)
    report = analyze(topology)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(format_text_report(report))

    # Non-zero exit when a prescribed boundary was mixed into another domain.
    return 1 if report.has_leak else 0


if __name__ == "__main__":
    sys.exit(main())
