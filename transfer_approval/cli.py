"""Command line interface for local JSON approval cases."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .engine import ApprovalInputError, evaluate_case


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate an offline hand-over key-commitment case from local JSON data."
    )
    parser.add_argument(
        "case_path",
        nargs="?",
        help="Path to a JSON case file. Omit it or use --stdin to read standard input.",
    )
    parser.add_argument("--stdin", action="store_true", help="Read the JSON case from stdin.")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON instead of indented JSON.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.case_path and not args.stdin:
            raw = Path(args.case_path).read_text(encoding="utf-8")
        else:
            raw = sys.stdin.read()
        case = json.loads(raw)
        report = evaluate_case(case)
    except (OSError, json.JSONDecodeError, ApprovalInputError) as exc:
        error = {"status": "invalid_input", "error": str(exc)}
        print(json.dumps(error, ensure_ascii=False, indent=2))
        return 2

    if args.compact:
        json.dump(report, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    else:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")

    if report["status"] == "certain":
        return 0
    if report["status"] == "ambiguous":
        return 3
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
