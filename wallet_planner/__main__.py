"""Command-line front end.

Usage::

    python3 -m wallet_planner SPEC.json            # print the plan as JSON
    python3 -m wallet_planner --strict SPEC.json   # exit 1 if unsatisfiable
    python3 -m wallet_planner -                   # read the spec from stdin

The specification is read as JSON.  Nothing is fetched from the network and no
files are written, so the tool is safe to run on untrusted input as long as the
input file itself is not executed (it is only parsed).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .model import SpecError
from .planner import UnsatisfiableError, plan_presentation


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wallet_planner",
        description="Plan a minimal-disclosure credential presentation.",
    )
    parser.add_argument(
        "spec",
        help="path to a JSON specification, or '-' to read standard input",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit with status 1 when no single-holder presentation exists",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation for the output (default: 2)",
    )
    args = parser.parse_args(argv)

    try:
        if args.spec == "-":
            raw = json.load(sys.stdin)
        else:
            with open(args.spec, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
    except OSError as exc:
        print(f"error: cannot read {args.spec}: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON: {exc}", file=sys.stderr)
        return 2

    try:
        plan = plan_presentation(raw)
    except SpecError as exc:
        print(f"error: invalid specification: {exc}", file=sys.stderr)
        return 2

    print(plan.to_json(indent=args.indent, sort_keys=False))
    if args.strict and not plan.satisfied:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
