#!/usr/bin/env python3
"""CLI entry point for the parser regression summary.

Usage:
    python3 app.py --baseline BASE.json --current CUR.json [--format markdown|json]
                   [--output PATH]

Exit codes:
    0  summary produced
    1  summary produced but at least one *new* failure was found
    2  input could not be loaded/validated, or a usage error occurred
"""

import argparse
import sys

from parser_regression import (
    LoadError,
    compare_reports,
    load_report,
    render_json,
    render_markdown,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description=(
            "Compare a confirmed baseline test-result export with the current "
            "build export and describe new failures, fixes, persistent failures, "
            "unstable samples and unexecuted samples."
        ),
    )
    parser.add_argument("--baseline", required=True, metavar="PATH",
                        help="confirmed previous version's result export (JSON)")
    parser.add_argument("--current", required=True, metavar="PATH",
                        help="this build's exported structured test results (JSON)")
    parser.add_argument("--format", choices=("markdown", "json"),
                        default="markdown", help="output format (default: markdown)")
    parser.add_argument("--output", metavar="PATH",
                        help="write output to PATH instead of stdout")
    parser.add_argument("--exit-on-new-failure", action="store_true",
                        help="return exit code 1 when new failures are present "
                             "(default: still 0, since a summary was produced)")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        baseline = load_report(args.baseline)
    except LoadError as exc:
        print(f"error: baseline: {exc}", file=sys.stderr)
        return 2
    try:
        current = load_report(args.current)
    except LoadError as exc:
        print(f"error: current: {exc}", file=sys.stderr)
        return 2

    summary = compare_reports(baseline, current)
    rendered = render_json(summary) if args.format == "json" else render_markdown(summary)

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(rendered)
        except OSError as exc:
            print(f"error: cannot write {args.output}: {exc}", file=sys.stderr)
            return 2
    else:
        sys.stdout.write(rendered)

    counts = summary["counts"]
    print(
        "summary: "
        f"new_failures={counts['new_failures']} "
        f"fixed={counts['fixed']} "
        f"persistent={counts['persistent_failures']} "
        f"unstable={counts['unstable']} "
        f"not_executed={counts['not_executed']} "
        f"needs_confirmation={counts['needs_confirmation']}",
        file=sys.stderr,
    )

    if args.exit_on_new_failure and counts["new_failures"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
