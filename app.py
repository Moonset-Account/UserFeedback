#!/usr/bin/env python3
"""Patch coverage matrix CLI.

Cross-references four records — entry-point list, module call graph,
patch checkpoints and observed test traces — and reports, for every call
entry point, whether the patch verification statically reaches it, has
observed coverage for it, or has test results that cannot be attributed.

Usage:
    python3 app.py --entries entries.json --graph graph.json \
        --checkpoints checkpoints.json --traces traces.json \
        [--build BUILD_ID] [--format text|json] [--json-out PATH] \
        [--fail-on-uncovered]

Exit codes: 0 = report generated (and, with --fail-on-uncovered, no
uncovered entries); 1 = uncovered entries present under
--fail-on-uncovered; 2 = input/usage error.
"""

import argparse
import json
import sys

from covmatrix import analyzer, loader, report as report_mod


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Generate the patch-verification coverage matrix for call entry points.",
    )
    parser.add_argument("--entries", required=True, help="entry-point list JSON")
    parser.add_argument("--graph", required=True, help="module call graph JSON")
    parser.add_argument("--checkpoints", required=True, help="patch checkpoints JSON")
    parser.add_argument("--traces", required=True, help="observed test traces JSON")
    parser.add_argument(
        "--build",
        default=None,
        help="target build id; defaults to the build recorded in the checkpoints file",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="stdout format (default: text)",
    )
    parser.add_argument("--json-out", default=None, help="also write the JSON report to PATH")
    parser.add_argument(
        "--fail-on-uncovered",
        action="store_true",
        help="exit 1 when any entry remains uncovered",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        entries = loader.load_entries(args.entries)
        graph = loader.load_graph(args.graph)
        cp_build, checkpoints = loader.load_checkpoints(args.checkpoints)
        traces = loader.load_traces(args.traces)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    build = args.build or cp_build
    if args.build and args.build != cp_build:
        print(
            f"error: --build {args.build!r} does not match checkpoints build {cp_build!r}",
            file=sys.stderr,
        )
        return 2

    analysis = analyzer.analyze(entries, graph, checkpoints, traces, build)
    report = report_mod.build_report(
        analysis,
        sources={
            "entries": args.entries,
            "graph": args.graph,
            "checkpoints": args.checkpoints,
            "traces": args.traces,
        },
    )

    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(report_mod.render_text(report))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

    if args.fail_on_uncovered and report["uncovered_entries"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
