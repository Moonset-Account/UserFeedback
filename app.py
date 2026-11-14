#!/usr/bin/env python3
"""Command line entry point for the cloud anomaly remediation planner.

Usage:
    python3 app.py run <scenario.json> [--output result.json]
    python3 app.py demo [--output result.json]

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import json
import sys

from remediation.scenario import build_demo_scenario, run_scenario


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cloud anomaly remediation planner with team daily impact budgets"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="execute a JSON scenario file")
    run_p.add_argument("scenario", help="path to the scenario JSON file")
    run_p.add_argument("--output", help="optional path to write the result JSON")

    demo_p = sub.add_parser("demo", help="run the built-in cross-day demo scenario")
    demo_p.add_argument("--output", help="optional path to write the result JSON")

    args = parser.parse_args(argv)

    try:
        if args.command == "demo":
            scenario = build_demo_scenario()
            result = run_scenario(scenario)
            result["scenario"] = "<built-in demo>"
        else:
            with open(args.scenario, "r", encoding="utf-8") as handle:
                scenario = json.load(handle)
            result = run_scenario(scenario)
            result = {"scenario": args.scenario, **result}
    except (ValueError, KeyError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"result written to {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
