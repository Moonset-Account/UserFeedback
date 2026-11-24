"""Local synthetic demonstration of role-specific allocation outputs.

Run from the workspace root:

    python3 demo.py
"""

from __future__ import annotations

import json

from forensic_allocator import (
    Exhibit,
    Reviewer,
    allocate,
    dispatch_view,
    reviewer_task_payload,
)


def main() -> None:
    exhibits = [
        Exhibit("E-1001", frozenset({"DNA"}), "SRC-A", priority=100),
        Exhibit("E-1002", frozenset({"DNA"}), "SRC-B", priority=90),
        Exhibit(
            "E-1003",
            frozenset({"DNA", "DIGITAL-EVIDENCE"}),
            "SRC-C",
            priority=80,
        ),
    ]
    reviewers = [
        Reviewer(
            "r1",
            "LAB-NORTH",
            frozenset({"DNA"}),
            capacity=1,
            name="Zhang San",
            exposed_source_groups=frozenset({"SRC-A"}),
        ),
        Reviewer("r2", "LAB-SOUTH", frozenset({"DNA"}), capacity=1, name="Li Si"),
        Reviewer("r3", "LAB-EAST", frozenset({"DNA"}), capacity=1, name="Wang Wu"),
        Reviewer("r4", "LAB-WEST", frozenset({"DNA"}), capacity=1, name="Zhao Liu"),
    ]

    result = allocate(exhibits, reviewers, anonymous_secret="local-demo-secret")

    print("Reviewer-facing payloads (safe for the frontend):")
    for reviewer in reviewers:
        print(
            reviewer.reviewer_id,
            json.dumps(
                reviewer_task_payload(result, reviewer.reviewer_id),
                ensure_ascii=False,
            ),
        )

    print("\nAuthorized dispatch view:")
    view = dispatch_view(result)
    print(
        json.dumps(
            {
                "coverage_count": view["coverage_count"],
                "coverage_exhibit_ids": view["coverage_exhibit_ids"],
                "assignments": view["assignments"],
                "unassigned": view["unassigned"],
                "reviewer_loads": view["reviewer_loads"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
