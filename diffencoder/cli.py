"""Command line entry point for the constrained differential encoder.

Examples
--------

Encode::

    python -m diffencoder encode --source base.bin --target target.bin \\
        --interval 0:128 --interval 256:512 \\
        --copy-cost 5 --literal-head 2 --literal-byte 1 --out plan.json

Decode / verify a plan::

    python -m diffencoder decode --source base.bin --plan plan.json --out target.bin
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence, Tuple

from .encoder import CopyOp, LiteralOp, Plan, decode, encode


def _parse_interval(text: str) -> Tuple[int, int]:
    if ":" not in text:
        raise argparse.ArgumentTypeError(
            f"interval {text!r} must look like START:END (half-open)"
        )
    start_text, end_text = text.split(":", 1)
    try:
        start, end = int(start_text, 0), int(end_text, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"interval {text!r} has non-integer bounds") from exc
    return start, end


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diffencoder",
        description="Minimum-cost constrained differential encoder",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    enc = sub.add_parser("encode", help="build an update plan")
    enc.add_argument("--source", required=True, help="path to the old base package")
    enc.add_argument("--target", required=True, help="path to the approved target bytes")
    enc.add_argument(
        "--interval",
        action="append",
        default=[],
        type=_parse_interval,
        metavar="START:END",
        help="public half-open byte range; repeat for multiple intervals",
    )
    enc.add_argument("--copy-cost", type=float, required=True)
    enc.add_argument("--literal-head", type=float, required=True)
    enc.add_argument("--literal-byte", type=float, default=1.0)
    enc.add_argument("--out", help="write the JSON plan here (default: stdout)")

    dec = sub.add_parser("decode", help="rebuild target bytes from a plan")
    dec.add_argument("--source", required=True, help="path to the old base package")
    dec.add_argument("--plan", required=True, help="path to a JSON plan produced by encode")
    dec.add_argument("--out", help="write rebuilt bytes here (default: stdout)")

    return parser


def _plan_from_json(payload: dict) -> Plan:
    ops = []
    for raw in payload["ops"]:
        kind = raw["op"]
        if kind == "copy":
            ops.append(CopyOp(offset=int(raw["offset"]), length=int(raw["length"])))
        elif kind == "literal":
            ops.append(LiteralOp(data=bytes.fromhex(raw["data"])))
        else:
            raise ValueError(f"unknown op kind {kind!r}")
    costs = payload.get("costs", {})
    return Plan(
        ops=tuple(ops),
        cost=float(payload.get("cost", 0.0)),
        source_length=int(payload["source_length"]),
        target_length=int(payload["target_length"]),
        public_intervals=tuple(
            (int(s), int(e)) for s, e in payload.get("public_intervals", [])
        ),
        copy_cost=float(costs.get("copy", 0.0)),
        literal_head=float(costs.get("literal_head", 0.0)),
        literal_byte=float(costs.get("literal_byte", 1.0)),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "encode":
        with open(args.source, "rb") as handle:
            source = handle.read()
        with open(args.target, "rb") as handle:
            target = handle.read()
        plan = encode(
            source=source,
            public_intervals=args.interval,
            target=target,
            copy_cost=args.copy_cost,
            literal_head=args.literal_head,
            literal_byte=args.literal_byte,
        )
        rendered = json.dumps(plan.to_jsonable(), indent=2, ensure_ascii=False)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(rendered)
                handle.write("\n")
        else:
            sys.stdout.write(rendered)
            sys.stdout.write("\n")
        return 0

    with open(args.source, "rb") as handle:
        source = handle.read()
    with open(args.plan, "r", encoding="utf-8") as handle:
        plan = _plan_from_json(json.load(handle))
    rebuilt = decode(plan, source=source, public_intervals=plan.public_intervals)
    if args.out:
        with open(args.out, "wb") as handle:
            handle.write(rebuilt)
    else:
        sys.stdout.buffer.write(rebuilt)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
