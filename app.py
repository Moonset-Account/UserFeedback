#!/usr/bin/env python3
"""CLI entry point for the synthetic credential marker scanner.

Usage:
    python3 app.py BUILD_DIR MARKERS_FILE [--chunk-size N] [--max-bytes N]
                                          [--output REPORT.json]

Exit codes (for CI gating):
    0  scan completed, no marker found, no scan errors
    1  at least one marker occurrence was found
    2  scan could not fully run (bad arguments, unreadable markers file,
       or files with corrupt/unreadable coverage status)
"""

import argparse
import json
import os
import sys

from scanner import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MAX_BYTES,
    build_report,
    load_markers,
    scan_directory,
)

EXIT_CLEAN = 0
EXIT_MATCHES = 1
EXIT_ERROR = 2


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scan a build directory for synthetic credential markers "
                    "in text and gzip-compressed text files."
    )
    parser.add_argument("build_dir", help="local build directory to scan")
    parser.add_argument("markers_file",
                        help="text file with one synthetic marker per line; "
                             "the line number is the marker id in the report")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                        help="read chunk size in bytes (default: %(default)s)")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help="per-file scan limit in (decompressed) bytes "
                             "(default: %(default)s)")
    parser.add_argument("--output",
                        help="also write the JSON report to this path")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.build_dir):
        print("error: build directory not found: %s" % args.build_dir,
              file=sys.stderr)
        return EXIT_ERROR
    if args.chunk_size < 1:
        print("error: --chunk-size must be >= 1", file=sys.stderr)
        return EXIT_ERROR
    if args.max_bytes < 0:
        print("error: --max-bytes must be >= 0", file=sys.stderr)
        return EXIT_ERROR

    try:
        markers = load_markers(args.markers_file)
    except OSError as exc:
        print("error: cannot read markers file: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    if not markers:
        print("error: markers file contains no markers", file=sys.stderr)
        return EXIT_ERROR

    reports = scan_directory(args.build_dir, markers,
                             chunk_size=args.chunk_size,
                             max_bytes=args.max_bytes)
    report = build_report(args.build_dir, markers, reports)
    text = json.dumps(report, indent=2)
    print(text)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

    summary = report["summary"]
    if summary["matches_total"]:
        return EXIT_MATCHES
    if summary["files_corrupt"] or summary["files_unreadable"]:
        return EXIT_ERROR
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
