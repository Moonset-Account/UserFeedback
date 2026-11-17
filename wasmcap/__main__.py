"""Command line interface: ``python -m wasmcap analyze <summary.json>``."""
from __future__ import annotations

import argparse
import json
import sys

from .analyze import analyze, analyze_entry
from .model import SummaryError, load_summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="wasmcap",
        description="Analyze which host capabilities a Wasm plugin may exercise.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_analyze = sub.add_parser("analyze", help="analyze a builder-exported summary JSON")
    p_analyze.add_argument("summary", help="path to the summary JSON file")
    p_analyze.add_argument("--entry", help="only analyze the named entry")
    args = parser.parse_args(argv)

    try:
        summary = load_summary(args.summary)
    except (SummaryError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 2

    if args.command == "analyze":
        if args.entry:
            for entry in summary.entries:
                if entry.name == args.entry:
                    report = analyze_entry(summary, entry)
                    break
            else:
                print(json.dumps({"error": f"unknown entry {args.entry!r}"}),
                      file=sys.stderr)
                return 2
        else:
            report = analyze(summary)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
