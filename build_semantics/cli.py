"""Command-line entry point for the build execution semantics library.

Usage:
    python -m build_semantics.cli analyze <pipeline.json>
    python -m build_semantics.cli resume <pipeline.json> <snapshot.json>
    python -m build_semantics.cli replay <pipeline.json> <event> [<event> ...]

The command prints a JSON report; it reads only local files and never
performs network access.
"""

from __future__ import annotations

import json
import sys
from typing import List, Sequence

from .loader import pipeline_from_file, snapshot_from_dict
from .model import AnalysisResult, ReplayOutcome, analyze, analyze_snapshot, replay


def _emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    command, rest = args[0], args[1:]
    try:
        if command == "analyze" and len(rest) == 1:
            result: AnalysisResult = analyze(pipeline_from_file(rest[0]))
            _emit(result.to_dict())
            return 0
        if command == "resume" and len(rest) == 2:
            pipeline = pipeline_from_file(rest[0])
            with open(rest[1], "r", encoding="utf-8") as handle:
                snapshot = snapshot_from_dict(pipeline, json.load(handle))
            _emit(analyze_snapshot(snapshot).to_dict())
            return 0
        if command == "replay" and len(rest) >= 2:
            outcome: ReplayOutcome = replay(pipeline_from_file(rest[0]), rest[1:])
            _emit(outcome.to_dict())
            return 0
    except Exception as exc:  # surface library errors as JSON-ish text
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(__doc__, file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
