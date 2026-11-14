#!/usr/bin/env python3
"""CLI entry for the mirror-collector decommission precheck.

Usage:
    python3 app.py SNAPSHOT.json [--decommission COLLECTOR_ID ...] [--compact]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import analyze, simulate  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="List, per link direction, whether mirror coverage "
                    "observes it; optionally simulate decommissioning "
                    "collectors on the same snapshot.")
    parser.add_argument("snapshot", help="path to the snapshot JSON file")
    parser.add_argument("--decommission", action="append", default=[],
                        metavar="COLLECTOR_ID",
                        help="collector to simulate as decommissioned "
                             "(repeatable)")
    parser.add_argument("--compact", action="store_true",
                        help="emit compact single-line JSON")
    args = parser.parse_args(argv)

    try:
        with open(args.snapshot, "r", encoding="utf-8") as fh:
            snapshot = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print("error: cannot load snapshot %s: %s" % (args.snapshot, exc),
              file=sys.stderr)
        return 2

    try:
        result = {"directions": analyze(snapshot)}
        if args.decommission:
            result["simulation"] = simulate(snapshot, args.decommission)
    except (ValueError, KeyError) as exc:
        print("error: invalid snapshot: %s" % exc, file=sys.stderr)
        return 2

    json.dump(result, sys.stdout, indent=None if args.compact else 2,
              ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
