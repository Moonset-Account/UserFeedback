#!/usr/bin/env python3
"""Local importer entry point.

Usage:
    python3 app.py import INPUT [--limits LIMITS.json] [--report OUT.json]
                                      [--pretty | --compact] [--quiet]

Exit codes:
    0  every record was accepted
    1  at least one record was rejected; other records were still processed
    2  the input file itself was unusable (unreadable / too large / not UTF-8)

The command never writes to the input and never touches the network.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn

from importer import core, limits as limits_module

EXIT_USAGE = 64


class _Parser(argparse.ArgumentParser):
    """Argument errors exit 64 so that 2 always means 'input unusable'."""

    def error(self, message: str) -> NoReturn:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="app.py",
        description="Whitelist-based local importer for versioned task records.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser(
        "import", help="decode a JSON Lines or JSON array file of records"
    )
    run.add_argument("input", help="input file (JSONL by default, JSON array if it starts with '[')")
    run.add_argument(
        "--limits",
        metavar="FILE",
        help="JSON object overriding any of: max_file_bytes, max_record_bytes, "
        "max_object_depth, max_list_items",
    )
    run.add_argument("--report", metavar="FILE", help="also write the JSON report here")
    format_group = run.add_mutually_exclusive_group()
    format_group.add_argument(
        "--pretty", dest="format", action="store_const", const="pretty", help="indented JSON report (default)"
    )
    format_group.add_argument(
        "--compact", dest="format", action="store_const", const="compact", help="single-line JSON report"
    )
    run.set_defaults(format="pretty")
    run.add_argument(
        "--quiet", action="store_true", help="suppress the report on stdout; use with --report"
    )
    run.set_defaults(handler=_run_import)

    return parser


def _write_stdout(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _run_import(args: argparse.Namespace) -> int:
    try:
        boundary = limits_module.load_limits(args.limits)
    except limits_module.LimitsConfigError as exc:
        print(f"error: limits configuration rejected: {exc}", file=sys.stderr)
        return EXIT_USAGE

    report = core.import_file(args.input, limits=boundary)

    if args.format == "compact":
        rendered = json.dumps(report, ensure_ascii=False) + "\n"
    else:
        rendered = core.render(report) + "\n"

    if not args.quiet:
        _write_stdout(rendered)

    if args.report:
        try:
            Path(args.report).write_text(rendered, encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write report: {exc}", file=sys.stderr)
            return EXIT_USAGE

    return core.exit_code(report)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except BrokenPipeError:  # pragma: no cover - shell plumbing
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
