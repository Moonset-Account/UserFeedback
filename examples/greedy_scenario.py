"""Run the access-distance vs early-writeback counterexample.

Run from the repository root:
    python3 examples/greedy_scenario.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blockcache import (  # noqa: E402
    CachePlanner,
    InitialResident,
    Page,
    Pin,
    Read,
    TickInterpreter,
    Write,
    access_distance_greedy_plan,
)


def main() -> None:
    pages = [
        Page("A", 1),
        Page("M", 2),
        Page("B", 3),
        Page("X", 4),
        Page("L", 5),
    ]
    planner = CachePlanner(
        pages,
        capacity=11,
        read_miss_ticks=5,
        writeback_ticks=3,
        initial_cache={
            "A": InitialResident(),
            "B": InitialResident(),
            "L": InitialResident(),
        },
    )
    requests = [
        Pin("A"),
        Pin("B"),
        Write("L", "dirty-late-page"),
        Read("M"),
        Read("X"),
        Read("M"),
        Read("A"),
        Read("B"),
        Read("L"),
    ]

    optimal = planner.plan(requests)
    greedy = access_distance_greedy_plan(planner, requests)
    optimal_report = TickInterpreter(planner, requests).run_all(optimal)
    greedy_report = TickInterpreter(planner, requests).run_all(greedy)

    print("optimal:", optimal.summary())
    print("greedy:", greedy.makespan_ticks, "ticks", greedy.writeback_bytes, "bytes")
    print("optimal verification ok:", optimal_report.ok)
    print("greedy verification ok:", greedy_report.ok)
    print("writebacks:")
    for wb in optimal.writebacks:
        print(
            " ",
            wb.page,
            "gen",
            wb.generation,
            "[",
            wb.started_at_tick,
            ",",
            wb.completed_at_tick,
            ")",
            "stale=",
            wb.stale_completion,
        )


if __name__ == "__main__":
    main()
