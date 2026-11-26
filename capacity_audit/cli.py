"""Command-line entry point: ``python3 -m capacity_audit --demo``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .analyzer import analyze
from .reporting import format_report
from .synthetic import build_synthetic_scenario


def _parse_clock_bounds(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    raise ValueError("clock_bounds in JSON input must be an object")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile load-test arrivals, throttles, completions, "
        "withdrawals, reissue batches, and capacity loss."
    )
    parser.add_argument("--events", type=Path, help="JSON event file")
    parser.add_argument("--window-width", type=float, default=None)
    parser.add_argument("--window-origin", type=float, default=None)
    parser.add_argument("--exhaustion-at", type=float, default=None)
    parser.add_argument("--json", action="store_true", help="print JSON result")
    parser.add_argument("--demo", action="store_true", help="run local synthetic data")
    args = parser.parse_args(argv)

    if args.demo or args.events is None:
        scenario = build_synthetic_scenario()
        result = analyze(
            scenario["events"],
            window_width=args.window_width or scenario["window_width"],
            window_origin=args.window_origin if args.window_origin is not None else 0.0,
            clock=scenario["clock"],
            exhaustion_at=(
                args.exhaustion_at
                if args.exhaustion_at is not None
                else scenario["exhaustion_at"]
            ),
            clock_bounds=scenario["clock_bounds"],
        )
    else:
        # The user explicitly passes a path in their own workspace; no network.
        data = json.loads(Path(args.events).read_text(encoding="utf-8"))
        if isinstance(data, list):
            events = data
            options: dict[str, Any] = {}
        elif isinstance(data, dict):
            events = data.get("events", [])
            options = {k: v for k, v in data.items() if k != "events"}
        else:
            raise SystemExit("JSON input must be a list or an object with events")
        json_width = options.pop("window_width", 10.0)
        json_origin = options.pop("window_origin", 0.0)
        json_exhaustion = options.pop("exhaustion_at", None)
        json_settle = options.pop("settle_at", None)
        width = args.window_width or json_width
        origin = args.window_origin if args.window_origin is not None else json_origin
        exhaustion = (
            args.exhaustion_at
            if args.exhaustion_at is not None
            else (json_exhaustion if json_exhaustion is not None else json_settle)
        )
        result = analyze(
            events,
            window_width=width,
            window_origin=origin,
            exhaustion_at=exhaustion,
            **options,
        )

    if args.json:
        json.dump(result.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
