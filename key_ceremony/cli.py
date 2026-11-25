from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .api import schedule_ceremony
from .errors import CeremonyInputError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m key_ceremony",
        description="Schedule independent offline-key custodians and handovers.",
    )
    parser.add_argument(
        "spec",
        nargs="?",
        help="Path to a JSON specification. Omit it to read JSON from stdin.",
    )
    parser.add_argument(
        "--handover-trigger",
        choices=("both_seats", "disjoint_crew"),
        default="both_seats",
        help="When a handover is required (default: both fixed seats changed).",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation (default: 2).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.spec:
            result = schedule_ceremony(Path(args.spec), handover_trigger=args.handover_trigger)
        else:
            text = sys.stdin.read()
            result = schedule_ceremony(text, handover_trigger=args.handover_trigger)
        payload: dict[str, Any] = result.to_dict()
        print(json.dumps(payload, ensure_ascii=False, indent=args.indent))
        return 0 if result.feasible else 2
    except (CeremonyInputError, json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"feasible": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
