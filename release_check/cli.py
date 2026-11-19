"""Command line entry point for the release checker.

Usage::

    python -m release_check SPEC.json
    python -m release_check SPEC.json --format json
    python -m release_check SPEC.json --substitute IFeedReader Candidate
    cat SPEC.json | python -m release_check -

Exit codes
----------
``0``  every checked entry is decidable and no violation was found.
``1``  at least one violation (the release is rejected).
``2``  the run could not decide everything: undecided entries remain, or the
       specification itself is invalid.
"""

from __future__ import annotations

import argparse
import sys

from .checker import Report, check, check_substitution
from .model import InputError, load_document, parse_spec
from .report import render_json, render_text

__all__ = ["main"]

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_UNDECIDED = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_check",
        description=(
            "Check candidate implementations against per-identity interface "
            "contracts, including diamond inheritance."
        ),
    )
    parser.add_argument(
        "spec",
        help="path to the JSON specification, or '-' to read standard input",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )
    parser.add_argument(
        "--substitute",
        nargs=2,
        metavar=("INTERFACE", "IMPLEMENTATION"),
        help="only check that IMPLEMENTATION may be called through INTERFACE",
    )
    parser.add_argument(
        "--exact",
        action="store_true",
        help=(
            "also require every granted field to be returned, instead of only "
            "forbidding fields outside the visible range"
        ),
    )
    return parser


def _exit_code(report: Report) -> int:
    if report.findings:
        return EXIT_REJECTED
    if report.pending:
        return EXIT_UNDECIDED
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)

    try:
        document = load_document(arguments.spec)
        spec = parse_spec(document)
    except InputError as exc:
        print(f"invalid specification: {exc}", file=sys.stderr)
        return EXIT_UNDECIDED

    try:
        if arguments.substitute:
            interface_name, implementation_name = arguments.substitute
            report = check_substitution(
                spec, interface_name, implementation_name, exact=arguments.exact
            )
            title = f"substitution check {interface_name} <- {implementation_name}"
        else:
            report = check(spec, exact=arguments.exact)
            title = "release check"
    except KeyError as exc:
        print(f"invalid specification: {exc.args[0]}", file=sys.stderr)
        return EXIT_UNDECIDED

    if arguments.format == "json":
        sys.stdout.write(render_json(report))
    else:
        sys.stdout.write(render_text(report, title=title))
    return _exit_code(report)


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
