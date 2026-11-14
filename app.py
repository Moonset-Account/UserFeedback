#!/usr/bin/env python3
"""CLI entry for the atomic safe ZIP batch importer.

Usage:
    python3 app.py plan   ARCHIVE [--platform P]
    python3 app.py import ARCHIVE SHARED_DIR [--max-entry-bytes N]
                          [--max-total-bytes N] [--batch-name NAME]
                          [--platform P]

All reports are JSON on stdout. Exit codes: 0 = success, 1 = import
rejected/failed (see the JSON error report), 2 = usage error.
"""

import argparse
import json
import os
import sys

# Keep the CLI runnable under `python3 -I` (isolated mode drops the
# script's directory from sys.path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from importer import (
    DEFAULT_MAX_ENTRY_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    ImportFailure,
    import_archive,
    plan_report,
)


def _parse_size(text):
    """Parse a byte limit: plain integer or K/M/G suffix (1024-based)."""
    s = text.strip().upper()
    mult = 1
    if s.endswith("K"):
        mult, s = 1024, s[:-1]
    elif s.endswith("M"):
        mult, s = 1024 ** 2, s[:-1]
    elif s.endswith("G"):
        mult, s = 1024 ** 3, s[:-1]
    try:
        value = int(s) * mult
    except ValueError:
        raise argparse.ArgumentTypeError("invalid size: %r" % text)
    if value < 0:
        raise argparse.ArgumentTypeError("size must be >= 0")
    return value


def _emit(report):
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_plan(args):
    try:
        report = plan_report(args.archive, platform=args.platform)
    except ImportFailure as failure:
        _emit(failure.to_report())
        return 1
    _emit(report)
    return 0


def cmd_import(args):
    try:
        report = import_archive(
            args.archive,
            args.shared_dir,
            max_entry_bytes=args.max_entry_bytes,
            max_total_bytes=args.max_total_bytes,
            batch_name=args.batch_name,
            platform=args.platform,
        )
    except ImportFailure as failure:
        _emit(failure.to_report())
        return 1
    _emit(report)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Atomic, safe batch import of ZIP resource packages "
                    "into a shared material directory.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser(
        "plan", help="list each entry's target relative path (no extraction)")
    p_plan.add_argument("archive", help="path to the ZIP archive")
    p_plan.add_argument(
        "--platform", default="auto",
        help="target filesystem rules for collision detection: "
             "auto|darwin|windows|linux (default: auto)")
    p_plan.set_defaults(func=cmd_plan)

    p_imp = sub.add_parser(
        "import", help="import the archive as one new batch directory")
    p_imp.add_argument("archive", help="path to the ZIP archive")
    p_imp.add_argument("shared_dir", help="allowed shared material directory "
                                          "(must already exist)")
    p_imp.add_argument("--max-entry-bytes", type=_parse_size,
                       default=DEFAULT_MAX_ENTRY_BYTES,
                       help="per-entry expanded byte limit, plain bytes or "
                            "K/M/G suffix (default: %(default)s)")
    p_imp.add_argument("--max-total-bytes", type=_parse_size,
                       default=DEFAULT_MAX_TOTAL_BYTES,
                       help="whole-package expanded byte limit, plain bytes "
                            "or K/M/G suffix (default: %(default)s)")
    p_imp.add_argument("--batch-name", default=None,
                       help="name of the new batch directory (default: "
                            "<archive-stem>-<content-hash>)")
    p_imp.add_argument("--platform", default="auto",
                       help="target filesystem rules for collision detection: "
                            "auto|darwin|windows|linux (default: auto)")
    p_imp.set_defaults(func=cmd_import)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
