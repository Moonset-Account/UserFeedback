"""Command-line entry point: ``python -m joinplan plan <file|->``.

Exit codes:
  0  plan produced
  2  invalid input (structure or cost model)
  3  input valid but no left-deep plan exists
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .model import InputError
from .parse import loads_query, load_query
from .planner import plan_query


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="joinplan",
        description="Compile an equi-join query into a minimum-transfer left-deep plan.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan", help="print the cheapest plan as JSON")
    plan_parser.add_argument(
        "input",
        help="path to a JSON request, or '-' to read from standard input",
    )
    plan_parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation for the output (default: 2)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.input == "-":
            query = loads_query(sys.stdin.read())
        else:
            query = load_query(args.input)
    except InputError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2

    try:
        plan = plan_query(query)
    except InputError as exc:
        print(f"no plan: {exc}", file=sys.stderr)
        return 3

    print(json.dumps(plan.to_dict(), indent=max(args.indent, 0), sort_keys=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
