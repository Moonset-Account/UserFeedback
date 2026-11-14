#!/usr/bin/env python3
"""CLI entry point for one deployment's secret reference resolution.

Usage:
    python3 app.py --environment ENV.json --services SERVICES.json \
        --catalog CATALOG.json [--format json|text]

Exit codes:
    0  every service plan is executable
    1  at least one service plan is blocked
    2  the inputs themselves are invalid
"""

from __future__ import annotations

import argparse
import json
import sys

from secretrefs import InputError, build_plan, plan_exit_code
from secretrefs.loader import load_inputs


def _render_text(plan: dict) -> str:
    lines: list[str] = []
    lines.append(f"environment: {plan['environment']}")
    overall = "EXECUTABLE" if plan["executable"] else "BLOCKED"
    lines.append(f"deployment plan: {overall}")
    for service in plan["services"]:
        state = "executable" if service["executable"] else "NOT EXECUTABLE"
        lines.append("")
        lines.append(f"service {service['service']}: {state}")
        for variable in service["variables"]:
            lines.append(
                f"  {variable['variable']} = {variable['resolved_ref']}"
                f"@v{variable['version']} [{variable['source_layer']}]"
            )
        for item in service["unresolved"]:
            marker = "BLOCKING" if item.get("blocking") else "warning"
            lines.append(
                f"  {item['variable']} UNRESOLVED ({item['reason']}, {marker}) "
                f"from {item['source_layer']}"
            )
            if "message" in item:
                lines.append(f"    {item['message']}")
        for conflict in service["conflicts"]:
            lines.append(f"  conflict: {conflict['message']}")
    if plan["same_name_collisions"]:
        lines.append("")
        lines.append("same-name variables fed by different sources:")
        for entry in plan["same_name_collisions"]:
            detail = ", ".join(
                f"{item['service']}={item['ref']}@v{item['version']} ({item['source_layer']})"
                for item in entry["services"]
            )
            lines.append(f"  {entry['variable']}: {detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve secret references for one deployment (identifiers and versions only)."
    )
    parser.add_argument("--environment", required=True, help="environment input JSON path")
    parser.add_argument("--services", required=True, help="services config JSON path")
    parser.add_argument("--catalog", required=True, help="secret reference catalog JSON path")
    parser.add_argument(
        "--format", choices=("json", "text"), default="json", help="output format (default: json)"
    )
    args = parser.parse_args(argv)

    try:
        inputs = load_inputs(args.environment, args.services, args.catalog)
    except InputError as exc:
        payload = {"error": "invalid_input", **exc.as_dict()}
        if args.format == "json":
            json.dump(payload, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        else:
            sys.stderr.write(f"invalid input: {exc.message}\n")
        return 2

    plan = build_plan(inputs)
    if args.format == "json":
        json.dump(plan, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(_render_text(plan) + "\n")
        for service in plan["services"]:
            for diag in service["diagnostics"]:
                if diag.get("chain"):
                    sys.stdout.write(
                        f"chain ({service['service']}/{diag['variable']}): "
                        + " -> ".join(
                            f"{s['environment']}/{s['service']}/{s['ref']}"
                            for s in diag["chain"]
                        )
                        + "\n"
                    )
    return plan_exit_code(plan)


if __name__ == "__main__":
    sys.exit(main())
