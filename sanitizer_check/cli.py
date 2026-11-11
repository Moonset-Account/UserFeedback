#!/usr/bin/env python3
"""Command line entry point for the pre-release sanitizer checker.

Usage:
    python -I -m sanitizer_check.cli \\
        --graph graph.json --old-rules rules-v1.json --new-rules rules-v2.json \\
        [--format text|json] [--output report.json] [--root main ...]

Exit codes:
    0  no newly allowed or newly blocked (newly undetermined) path
    1  at least one newly allowed / newly blocked path
    2  malformed input or analysis failure
"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import AnalysisError, LoaderError
from .report import render_text, run_check
from .verdict import NEWLY_ALLOWED, NEWLY_BLOCKED, UNDETERMINED_BUCKET


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Compare two sanitizer rule versions on one data-flow graph.")
    p.add_argument("--graph", required=True, help="path to the DFG JSON document")
    p.add_argument("--old-rules", required=True,
                   help="rule table JSON of the released version")
    p.add_argument("--new-rules", required=True,
                   help="rule table JSON of the candidate version")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.add_argument("--output", help="write the full JSON report here as well")
    p.add_argument("--root", action="append", default=[],
                   help="root function id (repeatable; default: all non-callees)")
    p.add_argument("--fail-on-undetermined", action="store_true",
                   help="also exit 1 when an item cannot be decided")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_check(args.graph, args.old_rules, args.new_rules,
                           roots=tuple(args.root))
    except (LoaderError, AnalysisError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot read input file: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: malformed JSON: {exc}", file=sys.stderr)
        return 2

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_text(report), end="")

    summary = report["summary"]
    if summary[NEWLY_ALLOWED] or summary[NEWLY_BLOCKED]:
        return 1
    if args.fail_on_undetermined and summary[UNDETERMINED_BUCKET]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
