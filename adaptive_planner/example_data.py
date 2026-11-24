"""Built-in small example satisfying the required common-probe trade-off."""

from __future__ import annotations

from typing import Any


def probe_example() -> dict[str, Any]:
    """Two correlated scenarios with different cheapest direct first joins.

    Scenario ``small-A`` prefers A-B first; ``small-C`` prefers B-C first.
    Materializing B is one common extra probe.  Its observed row count is 2 in
    one scenario and 20 in the other, allowing the strategy to select the
    cheaper first join and reduce the worst-case total from 303 to 161.
    """

    return {
        "schema_version": "adaptive-problem-1.0",
        "tables": ["A", "B", "C"],
        "edges": [
            {
                "id": "AB",
                "left": "A",
                "right": "B",
                "left_keys": ["A.k"],
                "right_keys": ["B.k"],
            },
            {
                "id": "BC",
                "left": "B",
                "right": "C",
                "left_keys": ["B.k"],
                "right_keys": ["C.bid"],
            },
        ],
        "scenarios": [
            {
                "id": "small-A",
                "rows": {
                    "A": 1,
                    "B": 2,
                    "C": 100,
                    "A+B": 2,
                    "B+C": 200,
                    "A+B+C": 200,
                },
            },
            {
                "id": "small-C",
                "rows": {
                    "A": 100,
                    "B": 20,
                    "C": 1,
                    "A+B": 2000,
                    "B+C": 20,
                    "A+B+C": 2000,
                },
            },
        ],
        "cost_model": {
            "join_costs": {
                "hash": {
                    "fixed": 0,
                    "per_left_row": 1,
                    "per_right_row": 1,
                    "per_output_row": 0,
                    "memory_per_left_row": 1,
                    "memory_per_right_row": 1,
                },
                "merge": {
                    "fixed": 1000,
                    "per_left_row": 1,
                    "per_right_row": 1,
                    "memory_per_left_row": 1,
                    "memory_per_right_row": 1,
                },
            },
            "materialization_cost": {
                "fixed": 0,
                "per_row": 1,
                "memory_per_row": 1,
            },
            "sort_cost": {
                "fixed": 100,
                "per_row": 100,
                "memory_per_row": 1,
            },
        },
        "default_memory_limit": 100000,
    }
