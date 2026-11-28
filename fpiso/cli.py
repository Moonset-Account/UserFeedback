"""Command-line entry point for the fpiso isolation model.

Examples
--------
Run every demonstration::

    python3 -m fpiso

Run one named scenario::

    python3 -m fpiso demo nested

Evaluate one operation in one domain::

    python3 -m fpiso op add 1 "1/16" --format small --rounding rne \\
        --masks inexact
"""

from __future__ import annotations

import argparse
import re
import sys
from fractions import Fraction

from .demo import all_scenarios
from .domain import Domain
from .errors import FpisoError
from .exact import as_exact
from .flags import FLAG_ORDER, MaskBits, flags_text
from .format import ROUNDING_ORDER, Format, coerce_rounding

#: Sentinel used to hide negative operands from argparse's option detection.
_ESCAPE = "\x00"

#: Options whose following token is a value (and must not be escaped).
_VALUE_OPTIONS = frozenset(
    {"--format", "--precision", "--emin", "--emax", "--rounding"}
)


def _escape_negative_operands(argv: list[str]) -> list[str]:
    """Let ``op add -1 2`` work without requiring an explicit ``--``.

    argparse treats any token starting with ``-`` as an option; an exact
    rational such as ``-5/4`` is not.  Such positional tokens are temporarily
    prefixed with a NUL sentinel, which the operand reader strips.  Tokens that
    are the value of a preceding option (``--emin -1``) are left alone.
    """

    escaped: list[str] = []
    previous: str | None = None
    for token in argv:
        is_operand = (
            previous not in _VALUE_OPTIONS
            and re.fullmatch(r"-[0-9][0-9A-Za-z./+-]*", token) is not None
        )
        escaped.append(_ESCAPE + token if is_operand else token)
        previous = token
    return escaped


def _unescape(token: str) -> str:
    return token[len(_ESCAPE):] if token.startswith(_ESCAPE) else token


def _format_from_args(args: argparse.Namespace) -> Format:
    return Format(
        name=args.format,
        precision=args.precision,
        emin=args.emin,
        emax=args.emax,
        has_infinity=not args.no_infinity,
    )


def _masks_from_args(values: list[str]) -> MaskBits:
    if not values:
        return MaskBits.all_masked()
    chosen = {str(value).lower() for value in values}
    unknown = chosen - {flag.value for flag in FLAG_ORDER}
    if unknown:
        raise SystemExit(f"unknown mask name(s): {', '.join(sorted(unknown))}")
    return MaskBits.unmasking(*chosen)


def _value_text(raw: str) -> Fraction:
    return as_exact(_unescape(raw))


def _cmd_demo(args: argparse.Namespace) -> int:
    scenarios = dict(all_scenarios())
    if args.name:
        if args.name not in scenarios:
            raise SystemExit(
                f"unknown scenario {args.name!r}; choose from "
                f"{', '.join(scenarios)}"
            )
        wanted = [(args.name, scenarios[args.name])]
    else:
        wanted = all_scenarios()
    for index, (_, function) in enumerate(wanted):
        if index:
            print()
        print("\n".join(function()))
    return 0


def _cmd_op(args: argparse.Namespace) -> int:
    fmt = _format_from_args(args)
    masks = _masks_from_args(args.masks)
    rounding = coerce_rounding(args.rounding)
    domain = Domain("cli", fmt, rounding, masks)

    if args.operation == "add":
        result = domain.add(_value_text(args.values[0]), _value_text(args.values[1]))
    elif args.operation == "mul":
        result = domain.mul(_value_text(args.values[0]), _value_text(args.values[1]))
    elif args.operation == "fma":
        trace = domain.fma(
            _value_text(args.values[0]),
            _value_text(args.values[1]),
            _value_text(args.values[2]),
        )
        print(f"exact product: {trace.product}")
        print(f"exact sum    : {trace.exact}")
        result = trace.result
    else:  # pragma: no cover - argparse restricts the choices
        raise SystemExit(f"unknown operation {args.operation!r}")

    print(f"domain : {domain.name} ({fmt.describe()})")
    print(f"round  : {rounding.value}")
    print(f"masks  : {masks.describe()}")
    print(f"value  : {result.value}")
    print(f"flags  : {flags_text(result.flags)}")
    print(f"sticky : {flags_text(domain.sticky)}")
    print(f"pending: [{', '.join(flag.value for flag in domain.pending)}]")
    return 0


def _cmd_formats(_: argparse.Namespace) -> int:
    from .format import SMALL

    print("built-in formats:")
    print(f"  {SMALL.describe()}")
    print()
    print("rounding attributes:")
    for mode in ROUNDING_ORDER:
        print(f"  {mode.value}")
    print()
    print("flags:")
    for flag in FLAG_ORDER:
        print(f"  {flag.value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fpiso",
        description="Executable isolation-semantics model for floating point.",
    )
    sub = parser.add_subparsers(dest="command")

    demo = sub.add_parser("demo", help="run scripted demonstrations")
    demo.add_argument("name", nargs="?", help="scenario name (default: all)")
    demo.set_defaults(handler=_cmd_demo)

    op = sub.add_parser("op", help="evaluate one operation in one domain")
    op.add_argument("operation", choices=["add", "mul", "fma"])
    op.add_argument(
        "values",
        nargs="+",
        help="exact operands, e.g. 1, 1/16, -5/4 (negative values are accepted)",
    )
    op.add_argument("--format", default="small", help="format name (default: small)")
    op.add_argument("--precision", type=int, default=4)
    op.add_argument("--emin", type=int, default=-2)
    op.add_argument("--emax", type=int, default=3)
    op.add_argument(
        "--rounding",
        default="rne",
        choices=[mode.value for mode in ROUNDING_ORDER],
    )
    op.add_argument(
        "--masks",
        nargs="*",
        default=None,
        help="flags to leave unmasked (default: none)",
    )
    op.add_argument(
        "--no-infinity",
        action="store_true",
        help="saturate overflow instead of producing infinity",
    )
    op.set_defaults(handler=_cmd_op)

    formats = sub.add_parser("formats", help="list built-in formats and modes")
    formats.set_defaults(handler=_cmd_formats)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        argv = ["demo"]
    args = parser.parse_args(_escape_negative_operands(argv))
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2
    try:
        return args.handler(args)
    except FpisoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
