"""Command line entry point: ``python -m change_scheduler``.

Reads a JSON problem from a file path argument or stdin and prints the plan.
This only formats the real scheduler output; it never fabricates results.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .model import Interval, Step
from .scheduler import plan_change, verify_plan


def _parse(raw: Any) -> tuple[list[Step], int]:
    if not isinstance(raw, dict):
        raise ValueError("input must be a JSON object")
    deadline = raw.get("deadline")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list):
        raise ValueError("'steps' must be a list")

    def interval(obj: Any, where: str) -> Interval:
        if (
            not isinstance(obj, list)
            or len(obj) != 2
            or not all(isinstance(x, int) and not isinstance(x, bool) for x in obj)
        ):
            raise ValueError(f"{where} must be [start, end] of integers")
        return Interval(obj[0], obj[1])

    steps: list[Step] = []
    for i, item in enumerate(steps_raw):
        if not isinstance(item, dict):
            raise ValueError(f"steps[{i}] must be an object")
        steps.append(
            Step(
                duration=item["duration"],
                window=interval(item["window"], f"steps[{i}].window"),
                rollback_duration=item["rollback_duration"],
                duty_window=interval(item["duty_window"], f"steps[{i}].duty_window"),
                name=str(item.get("name", "")),
            )
        )
    return steps, deadline


def _render(plan, steps) -> dict[str, Any]:
    return {
        "feasible": plan.feasible,
        "start_time": plan.start_time,
        "deadline": plan.deadline,
        "reason": plan.reason,
        "forward": [
            {"step": f.step_index, "name": f.name, "start": f.start, "end": f.end}
            for f in plan.forward
        ],
        "failures": [
            {
                "failed_step": fp.failed_step,
                "failure_time": fp.failure_time,
                "rollbacks": [
                    {
                        "step": r.step_index,
                        "name": r.name,
                        "start": r.start,
                        "end": r.end,
                    }
                    for r in fp.rollbacks
                ],
            }
            for fp in plan.failures
        ],
        "verification_problems": verify_plan(steps, plan.deadline, plan),
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) > 1:
        print("usage: python -m change_scheduler [problem.json]", file=sys.stderr)
        return 2
    try:
        if argv:
            with open(argv[0], "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        else:
            raw = json.load(sys.stdin)
        steps, deadline = _parse(raw)
        plan = plan_change(steps, deadline)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    json.dump(_render(plan, steps), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
