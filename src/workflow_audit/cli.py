"""Command-line entry point: analyze one job-graph JSON file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import analyze_graph
from .graph import GraphError, load_graph


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="workflow_audit",
        description="Propagate content origins through a JSON job graph and "
                    "report untrusted paths reaching release steps.",
    )
    parser.add_argument("graph", help="path to the job-graph JSON file")
    parser.add_argument(
        "-o", "--out",
        help="write the JSON report here instead of stdout",
    )
    args = parser.parse_args(argv)

    try:
        graph = load_graph(Path(args.graph))
        report = analyze_graph(graph)
    except (GraphError, json.JSONDecodeError, OSError) as exc:
        print(f"workflow_audit: error: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(report, indent=2, sort_keys=False, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
