#!/usr/bin/env python3
"""CLI entry point for the cross-build diagnostic symbol locator.

Usage:
    python3 app.py <input.json> [--json]

Reads local diagnostic records plus offline symbol tables from a single
JSON file and prints a grouping report (text by default, JSON with
--json).  See README.md for the input format and output semantics.
"""

import argparse
import json
import sys

from locator import build_report, render_text


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Resolve memory diagnostic call stacks against offline symbol "
            "tables and group records by error category and the first "
            "confirmable application frame."
        )
    )
    parser.add_argument("input", help="path to the diagnostic input JSON file")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the report as JSON instead of text",
    )
    args = parser.parse_args(argv)

    with open(args.input, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    report = build_report(payload)
    if args.json:
        json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
