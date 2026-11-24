"""Command-line entry point: ``python3 -m access_review batch.json``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .core import ReviewInputError, review_batch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review a permission-provisioning batch for entry/approval SoD conflicts."
    )
    parser.add_argument("path", help="Path to a JSON batch/request file")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Print compact JSON instead of indented JSON",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with status 3 when the review status is not approved",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        report = review_batch(payload)
    except (OSError, json.JSONDecodeError, ReviewInputError) as exc:
        print(f"access-review: error: {exc}", file=sys.stderr)
        return 2

    if args.compact:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.strict and report.get("status") != "approved":
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
