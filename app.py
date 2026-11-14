"""CLI entry point for the fixed-length digest comparison teaching example.

Usage:
    python3 app.py audit [--length N]

The ``audit`` command compares a baseline digest with itself and with one
derived digest per byte position, then prints, for every case group, only the
digest length, the trace comparison against the written full-scan spec, and a
pass/fail status.  No digest bytes are printed.

Exit status is 0 only when every case both yields the expected boolean and
matches the full-scan trace; otherwise it is 1.
"""

from __future__ import annotations

import argparse
import sys

import digest_compare
import trace_audit
from synthetic_cases import DEFAULT_LENGTH, boundary_cases, build_cases


def _run_audit(length: int) -> int:
    groups: list[tuple[str, list]] = [
        ("per-position", build_cases(length)),
        ("boundary", boundary_cases()),
    ]
    exit_code = 0
    print(
        f"spec={trace_audit.FULL_SCAN_SPEC} "
        f"digest_length={length} boundary_length=1"
    )
    for group_name, cases in groups:
        print(f"group={group_name}")
        records = trace_audit.audit_group(cases)
        for record in records:
            print("  " + record.report_line())
        print("  " + trace_audit.summarize(records))
        if any(not record.passed for record in records):
            exit_code = 1
    return exit_code


def _run_length_check(length: int) -> int:
    """Show that wrong-length inputs fail with a dedicated length error."""
    base = bytes(length)
    samples = [
        ("left-too-short", base[:-1], base),
        ("right-too-short", base, base[:-1]),
        ("left-empty", b"", base),
        ("both-too-long", base + b"\x00", base + b"\x00"),
    ]
    exit_code = 0
    print(f"length_check expected_length={length}")
    for sample_id, left, right in samples:
        try:
            digest_compare.compare_digests(left, right, length)
        except digest_compare.DigestLengthError as exc:
            print(
                f"  sample_id={sample_id} raised=DigestLengthError "
                f"lengths={dict(exc.actual_lengths)} status=PASS"
            )
        else:
            print(f"  sample_id={sample_id} raised=none status=FAIL")
            exit_code = 1
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fixed-length digest comparison trace audit (teaching example)"
    )
    subparsers = parser.add_subparsers(dest="command")
    audit = subparsers.add_parser("audit", help="compare per-position trace groups")
    audit.add_argument(
        "--length",
        type=int,
        default=DEFAULT_LENGTH,
        help=f"digest length to generate (default {DEFAULT_LENGTH})",
    )
    subparsers.add_parser("length-check", help="wrong-length inputs must be rejected")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "audit"
    if command == "audit":
        length = getattr(args, "length", DEFAULT_LENGTH)
        if length < 1:
            print(f"error: --length must be at least 1 (got {length})", file=sys.stderr)
            return 2
        return _run_audit(length)
    if command == "length-check":
        return _run_length_check(DEFAULT_LENGTH)
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
