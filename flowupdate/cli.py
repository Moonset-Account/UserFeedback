"""Command line interface for the loop-free next-hop update planner.

Usage::

    python -m flowupdate solve  problem.json [--max-round-size N]
    python -m flowupdate verify problem.json plan.json
    python -m flowupdate check  problem.json node [node ...]

``solve`` prints a JSON schedule.  ``verify`` replays a plan (a JSON list of
rounds, or an object with a ``rounds`` key) and reports success or failure.
``check`` reports whether one specific round is safe on the current old table.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from .model import ModelError, load_problem
from .solver import NoScheduleError, check_round, solve
from .verify import verify_schedule


def _load_plan(path: str) -> List[List[str]]:
    with open(path, "r", encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ModelError(f"invalid JSON in plan file {path!r}: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("rounds")
    if not isinstance(data, list):
        raise ModelError("plan file must contain a list of rounds")
    rounds: List[List[str]] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, list) or not all(isinstance(n, str) for n in item):
            raise ModelError(f"round {index} must be a list of node names")
        rounds.append(list(item))
    return rounds


def _cmd_solve(args: argparse.Namespace) -> int:
    old, new, _label = load_problem(args.problem)
    schedule = solve(old, new, max_round_size=args.max_round_size)
    print(json.dumps(schedule.as_dict(), indent=2))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    old, new, _label = load_problem(args.problem)
    rounds = _load_plan(args.plan)
    result = verify_schedule(old, new, rounds)
    print(result.describe())
    return 0 if result.ok else 1


def _cmd_check(args: argparse.Namespace) -> int:
    old, new, _label = load_problem(args.problem)
    verdict = check_round(old, args.nodes, new)
    payload = {
        "nodes": list(verdict.nodes),
        "safe": verdict.safe,
    }
    if not verdict.safe:
        payload["failing_subset"] = list(verdict.failing_subset)
        payload["unreachable"] = list(verdict.unreachable)
    print(json.dumps(payload, indent=2))
    return 0 if verdict.safe else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m flowupdate",
        description="Plan loop-free next-hop updates with the fewest rounds.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    solve_parser = sub.add_parser("solve", help="compute a minimum-round plan")
    solve_parser.add_argument("problem", help="JSON problem file")
    solve_parser.add_argument(
        "--max-round-size",
        type=int,
        default=9,
        help="maximum nodes updated in one round (default 9)",
    )
    solve_parser.set_defaults(func=_cmd_solve)

    verify_parser = sub.add_parser("verify", help="verify a plan file")
    verify_parser.add_argument("problem", help="JSON problem file")
    verify_parser.add_argument("plan", help="JSON plan file (list of rounds)")
    verify_parser.set_defaults(func=_cmd_verify)

    check_parser = sub.add_parser("check", help="check one round for safety")
    check_parser.add_argument("problem", help="JSON problem file")
    check_parser.add_argument("nodes", nargs="+", help="nodes to update together")
    check_parser.set_defaults(func=_cmd_check)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ModelError, NoScheduleError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
