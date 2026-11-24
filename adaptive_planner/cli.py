"""Command line entry points: solve, verify, and audit traces."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .model import PlannerError, load_problem
from .planner import plan_strategy
from .validator import audit_decision_traces, verify_strategy


def _write_json(payload: Any, output: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adaptive_planner",
        description="Exact minimax adaptive join strategy planner and offline auditor",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    solve = sub.add_parser("solve", help="solve a problem JSON file")
    solve.add_argument("problem", help="path to problem JSON")
    solve.add_argument("-o", "--output", help="write strategy JSON here")
    solve.add_argument(
        "--no-materialization",
        action="store_true",
        help="compute best non-adaptive/fixed join plan instead",
    )

    verify = sub.add_parser("verify", help="offline verify a strategy JSON file")
    verify.add_argument("problem", help="path to problem JSON")
    verify.add_argument("strategy", help="path to strategy JSON")
    verify.add_argument(
        "--check-optimal",
        action="store_true",
        help="also rerun the exact optimizer and compare worst/sum objectives",
    )

    audit = sub.add_parser("audit-traces", help="audit a decision-trace JSON file")
    audit.add_argument(
        "traces",
        help="JSON file mapping scenario ids to ordered decision steps",
    )

    demo = sub.add_parser(
        "example", help="print the built-in two-scenario probe example problem"
    )
    demo.add_argument("-o", "--output", help="write example problem JSON here")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "example":
            from .example_data import probe_example

            _write_json(probe_example(), args.output)
            return 0

        if args.command == "solve":
            problem = load_problem(args.problem)
            strategy = plan_strategy(
                problem, allow_materialization=not args.no_materialization
            )
            _write_json(strategy, args.output)
            return 0

        if args.command == "verify":
            problem = load_problem(args.problem)
            strategy = json.loads(Path(args.strategy).read_text(encoding="utf-8"))
            report = verify_strategy(
                problem, strategy, check_optimal=args.check_optimal
            )
            _write_json(report.to_dict(), None)
            return 0 if report.ok else 2

        if args.command == "audit-traces":
            traces = json.loads(Path(args.traces).read_text(encoding="utf-8"))
            report = audit_decision_traces(traces)
            _write_json(report.to_dict(), None)
            return 0 if report.ok else 2

    except (PlannerError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
