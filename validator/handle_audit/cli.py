#!/usr/bin/env python3
"""Command line entry point for the handle-authorization regression verifier.

Usage:
    python -m handle_audit.cli <event-log.json> [--json] [--out FILE]

Exit codes:
    0  PASS
    1  VIOLATION proven
    2  UNDECIDABLE (missing ordering evidence, e.g. lost sync records)
    3  input/schema error
"""

import argparse
import json
import sys

from .events import load_events
from .report import render_json, render_text
from .verifier import Verifier
from .findings import Verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that every committed write targets exactly the object a "
            "still-valid handle authorization validated, reconstructing "
            "order from thread seq and explicit sync edges."
        )
    )
    parser.add_argument("log", help="path to the structured JSON event log")
    parser.add_argument(
        "--json", action="store_true", help="emit a machine-readable report"
    )
    parser.add_argument(
        "--out", help="write the report here instead of stdout"
    )
    args = parser.parse_args(argv)

    try:
        with open(args.log, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        events = load_events(data)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 3

    verifier = Verifier(events)
    report = verifier.run()

    if args.json:
        text = render_json(report)
    else:
        text = render_text(report, events, verifier.graph)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)

    return {
        Verdict.PASS: 0,
        Verdict.VIOLATION: 1,
        Verdict.UNDECIDABLE: 2,
    }[report.verdict]


if __name__ == "__main__":
    sys.exit(main())
