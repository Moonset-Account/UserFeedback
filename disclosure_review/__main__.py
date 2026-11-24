"""Command-line entry point for JSON case files.

Example::

    python3 -m disclosure_review examples/synthetic_case.json --no-traces
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .api import review_dict


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review disclosure ranges from a local synthetic JSON case."
    )
    parser.add_argument("case_path", type=Path, help="Path to a case JSON file")
    parser.add_argument(
        "--no-traces",
        action="store_true",
        help="omit detailed citation traces from paragraph output",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact JSON instead of indented JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with args.case_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    report = review_dict(payload, include_trace=not args.no_traces)
    if args.compact:
        json.dump(report, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    else:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
