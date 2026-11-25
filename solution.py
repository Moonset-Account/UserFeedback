"""Convenience entry point for callers that expect a top-level ``solution`` module.

The implementation lives in the :mod:`key_ceremony` package.  This wrapper
exposes the most common functional API and a small JSON CLI.
"""

from __future__ import annotations

from typing import Any

from key_ceremony.api import (
    ScheduleResult,
    find_first_unreachable_round,
    schedule_ceremony,
    schedule_from_dict,
    schedule_from_json,
)
from key_ceremony.cli import main

__all__ = [
    "ScheduleResult",
    "find_first_unreachable_round",
    "schedule",
    "schedule_ceremony",
    "schedule_from_dict",
    "schedule_from_json",
    "solve",
]


def schedule(specification: Any, **kwargs: Any) -> ScheduleResult:
    """Return a :class:`~key_ceremony.api.ScheduleResult`."""
    return schedule_ceremony(specification, **kwargs)


def solve(specification: Any, **kwargs: Any) -> dict[str, Any]:
    """Return the JSON-serializable result dictionary."""
    return schedule_ceremony(specification, **kwargs).to_dict()


if __name__ == "__main__":
    raise SystemExit(main())
