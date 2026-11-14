#!/usr/bin/env python3
"""CLI entry point: attribute process starts to confirmed login sessions.

Usage:
    python3 app.py --sessions sessions_a.csv [--sessions sessions_b.csv ...]
                   --processes procs_a.jsonl [--processes procs_b.jsonl ...]
                   --calibration calibration.csv
                   [--output report.json]

The report is written as JSON to --output, or to stdout when omitted.
Exit code 0 on success, 2 on input/validation errors.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from attribution import build_report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Attribute process-start events to confirmed login "
                    "sessions using per-host/boot clock calibration.")
    parser.add_argument("--sessions", action="append", required=True,
                        metavar="CSV",
                        help="login/logout CSV export (repeatable)")
    parser.add_argument("--processes", action="append", required=True,
                        metavar="JSONL",
                        help="process-start JSONL export (repeatable)")
    parser.add_argument("--calibration", required=True, metavar="CSV",
                        help="clock calibration table (host,boot_id,"
                             "offset_seconds,error_seconds)")
    parser.add_argument("--output", metavar="PATH",
                        help="write JSON report here instead of stdout")
    args = parser.parse_args(argv)

    try:
        report = build_report(args.sessions, args.processes, args.calibration)
    except (ValueError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
