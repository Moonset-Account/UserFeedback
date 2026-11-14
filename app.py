#!/usr/bin/env python3
"""CLI entry point for the offline load-segment W^X page auditor.

Usage:
    python3 app.py <mapping-summary.json> [-o report.json] [--compact]

Reads a JSON document with per-build load-segment mapping summaries and
prints a JSON report listing, per build, the address pages that end up both
writable and executable after page alignment, together with the contributing
segments and the merge rule that was applied.  Builds with unrepresentable
addresses, invalid page sizes or missing mapping records are flagged with a
per-build error entry.

Exit codes: 0 = audit completed (per-build errors are data, see the report),
2 = the input file could not be read or parsed.
"""
import argparse
import json
import sys

from pageaudit import audit_document


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Offline auditor: find address pages that end up both "
                    "writable and executable after page alignment of load "
                    "segments.")
    parser.add_argument("input",
                        help="JSON file with per-build load-segment mapping "
                             "summaries")
    parser.add_argument("-o", "--output",
                        help="write the JSON report here instead of stdout")
    parser.add_argument("--compact", action="store_true",
                        help="emit compact single-line JSON")
    args = parser.parse_args(argv)

    try:
        with open(args.input, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as exc:
        print("app.py: cannot read %s: %s" % (args.input, exc),
              file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print("app.py: %s is not valid JSON: %s" % (args.input, exc),
              file=sys.stderr)
        return 2

    report = audit_document(doc)
    text = json.dumps(report, indent=None if args.compact else 2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
