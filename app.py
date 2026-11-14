#!/usr/bin/env python3
"""fwlayout-check: read-only firmware partition layout checker.

Usage:
    python3 app.py BOARD_JSON IMAGE [IMAGE ...] [--compact]

Reads a board description JSON and one or more firmware images, parses
only each image header / partition table, and prints a JSON report on
stdout.  Exit code 0 means the check ran (findings, if any, are data in
the report); exit code 2 means a usage/IO/board-description error.
"""

import argparse
import json
import sys

from fwlayout.board import BoardError, load_board
from fwlayout.report import check_images


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="fwlayout-check",
        description="Read-only firmware partition layout checker "
                    "(header and partition table only).")
    parser.add_argument("board", help="board description JSON file")
    parser.add_argument("images", nargs="+",
                        help="firmware image file(s) to check")
    parser.add_argument("--compact", action="store_true",
                        help="emit the JSON report without indentation")
    args = parser.parse_args(argv)

    try:
        board = load_board(args.board)
    except BoardError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    try:
        report = check_images(board, args.images)
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    json.dump(report, sys.stdout, indent=None if args.compact else 2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
