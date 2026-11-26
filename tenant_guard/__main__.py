"""Command line entry: ``python3 -m tenant_guard program.json``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .api import analyze_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check cache keys for unambiguous tenant provenance")
    parser.add_argument("program", help="Path to a JSON program description")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args(argv)

    report = analyze_file(Path(args.program))
    if args.pretty:
        print(report.to_json(indent=2))
    else:
        print(report.to_json(indent=None))
    return 0 if report.ok else 2


if __name__ == "__main__":
    sys.exit(main())
